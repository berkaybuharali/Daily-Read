// Unit tests for the pure logic in templates/library.js. Run by tests/test_library.py, or: node --test tests/library.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const L = require("../templates/library.js");

const T1 = "2026-10-01T10:00:00.000Z", T2 = "2026-10-02T10:00:00.000Z", T3 = "2026-10-03T10:00:00.000Z";
const snap = (id, extra) => Object.assign({ id, title: "Title " + id, url: "https://example.com/" + id, source: "Blog", published: "2026-10-01", stars: 4, verdict: "read", summary: "S" }, extra);

test("cleanRecord validates like the Python loader", () => {
  assert.equal(L.cleanRecord(null), null);
  assert.equal(L.cleanRecord({ id: "a" }), null);
  const r = L.cleanRecord(snap("a", { url: "javascript:alert(1)", stars: 9, verdict: "x", published: "yesterday", read_later: "yes" }));
  assert.equal(r.url, ""); assert.equal(r.stars, 0); assert.equal(r.verdict, ""); assert.equal(r.published, ""); assert.equal(r.read_later, false);
  assert.equal(L.cleanRecord(snap("a", { stars: true })).stars, 0);
  assert.equal(L.cleanRecord(snap("a", { title: "t".repeat(999) })).title.length, 300);
});

test("setFlag saves from a snapshot, sets timestamps, and does not mutate the old map", () => {
  const before = {};
  const m = L.setFlag(before, "a", snap("a"), "read_later", true, T1);
  assert.deepEqual(before, {});
  assert.equal(m.a.read_later, true); assert.equal(m.a.added_at, T1); assert.equal(m.a.updated_at, T1);
  const m2 = L.setFlag(m, "a", snap("a"), "favorite", true, T2);
  assert.equal(m2.a.favorite, true); assert.equal(m2.a.read_later, true); assert.equal(m2.a.added_at, T1); assert.equal(m2.a.updated_at, T2);
  assert.equal(L.setFlag({}, "z", { id: "z" }, "favorite", true, T1), null);      // unusable snapshot
});

test("removing from Read Later also clears read; marking read implies saved", () => {
  let m = L.setFlag({}, "a", snap("a"), "read_later", true, T1);
  m = L.setFlag(m, "a", null, "read", true, T2);
  assert.equal(m.a.read, true);
  m = L.setFlag(m, "a", null, "read_later", false, T3);
  assert.equal(m.a.read, false); assert.equal(m.a.read_later, false);
  const direct = L.setFlag({}, "b", snap("b"), "read", true, T1);
  assert.equal(direct.b.read_later, true);
});

test("a tombstone (in no list) keeps its record so the removal can win a merge", () => {
  let m = L.setFlag({}, "a", snap("a"), "favorite", true, T1);
  m = L.setFlag(m, "a", null, "favorite", false, T2);
  assert.ok(m.a); assert.equal(L.listOf(m, "favorites").length, 0);
});

test("mergeMaps: newest change wins per item; both sides' unique items survive", () => {
  const fileSide = L.setFlag({}, "a", snap("a"), "favorite", true, T1);
  const browser = L.setFlag(L.setFlag({}, "a", snap("a"), "favorite", true, T1), "a", null, "favorite", false, T2);   // removed later
  const merged = L.mergeMaps(fileSide, browser);
  assert.equal(merged.a.favorite, false);
  const onlyFile = L.setFlag({}, "f", snap("f"), "read_later", true, T1);
  const onlyBrowser = L.setFlag({}, "b", snap("b"), "read_later", true, T1);
  assert.deepEqual(Object.keys(L.mergeMaps(onlyFile, onlyBrowser)).sort(), ["b", "f"]);
  assert.deepEqual(L.mergeMaps(undefined, undefined), {});
});

test("mergeMaps: on a tie the second argument wins", () => {
  const a = { x: L.cleanRecord(snap("x", { title: "from a", updated_at: T1, read_later: true })) };
  const b = { x: L.cleanRecord(snap("x", { title: "from b", updated_at: T1, read_later: true })) };
  assert.equal(L.mergeMaps(a, b).x.title, "from b");
});

test("pruneMap drops old tombstones only", () => {
  const old = "2025-01-01T00:00:00.000Z";
  const map = {
    gone: L.cleanRecord(snap("gone", { updated_at: old })),
    keptOld: L.cleanRecord(snap("keptOld", { updated_at: old, favorite: true })),
    recentTomb: L.cleanRecord(snap("recentTomb", { updated_at: T3 })),
  };
  const out = L.pruneMap(map, Date.parse(T3));
  assert.deepEqual(Object.keys(out).sort(), ["keptOld", "recentTomb"]);
});

test("listOf sorts by article date, newest first; finished items sink in Read Later", () => {
  let m = {};
  m = L.setFlag(m, "old", snap("old", { published: "2026-09-01" }), "read_later", true, T1);
  m = L.setFlag(m, "new", snap("new", { published: "2026-10-03" }), "read_later", true, T1);
  m = L.setFlag(m, "mid", snap("mid", { published: "2026-09-20" }), "read_later", true, T1);
  assert.deepEqual(L.listOf(m, "later").map(r => r.id), ["new", "mid", "old"]);
  m = L.setFlag(m, "new", null, "read", true, T2);
  assert.deepEqual(L.listOf(m, "later").map(r => r.id), ["mid", "old", "new"]);
  m = L.setFlag(m, "old", null, "favorite", true, T2);
  m = L.setFlag(m, "new", null, "favorite", true, T2);
  assert.deepEqual(L.listOf(m, "favorites").map(r => r.id), ["new", "old"]);   // read does not matter in Favorites
});

test("listOf breaks date ties by when it was saved (latest first)", () => {
  let m = L.setFlag({}, "a", snap("a"), "read_later", true, T1);
  m = L.setFlag(m, "b", snap("b"), "read_later", true, T2);
  assert.deepEqual(L.listOf(m, "later").map(r => r.id), ["b", "a"]);
});

test("parseDoc accepts a library and rejects anything else", () => {
  assert.equal(L.parseDoc("{nope"), null);
  assert.equal(L.parseDoc("[]"), null);
  assert.equal(L.parseDoc('{"items": []}'), null);
  assert.equal(L.parseDoc(""), null);
  const doc = L.toDoc({ a: L.cleanRecord(snap("a", { read_later: true })) }, T1);
  const back = L.parseDoc(JSON.stringify(doc));
  assert.equal(back.a.title, "Title a");
  assert.deepEqual(L.parseDoc(JSON.stringify({ items: { bad: { id: "bad" } } })), {});
});

test("matches searches title, summary and source, case-insensitively", () => {
  const r = L.cleanRecord(snap("a", { title: "Rollback Does Not Erase", summary: "distributed memory", source: "SRE Weekly" }));
  assert.ok(L.matches(r, "")); assert.ok(L.matches(r, "ROLLBACK")); assert.ok(L.matches(r, "memory")); assert.ok(L.matches(r, "sre"));
  assert.ok(!L.matches(r, "kubernetes"));
});

test("text is truncated by code points, so an emoji is never cut in half", () => {
  const r = L.cleanRecord(snap("a", { title: "x".repeat(299) + "😀😀" }));
  assert.equal(Array.from(r.title).length, 300);
  assert.ok(!/[\ud800-\udbff](?![\udc00-\udfff])/.test(r.title), "no lone high surrogate");
  assert.equal(r.title.endsWith("😀"), true);
});

test("impossible and future timestamps are neutralised like the Python loader does", () => {
  assert.equal(L.cleanRecord(snap("a", { updated_at: "0000-00-00T99:99:99Z" })).updated_at, "1970-01-01T00:00:00.000Z");
  assert.equal(L.cleanRecord(snap("a", { updated_at: "2026-13-45T10:00:00.000Z" })).updated_at, "1970-01-01T00:00:00.000Z");
  const future = L.cleanRecord(snap("a", { updated_at: "9999-12-31T23:59:59.000Z" })).updated_at;
  assert.ok(Date.parse(future) <= Date.now() + 6 * 60000, "clamped to now + 5 minutes");
  assert.equal(L.cleanRecord(snap("a", { updated_at: "2026-10-03T10:00:00.000Z" })).updated_at, "2026-10-03T10:00:00.000Z");
});

test("a record added by pasting a link keeps its manual flag; only a real true counts", () => {
  assert.equal(L.cleanRecord(snap("a", { manual: true })).manual, true);
  assert.equal(L.cleanRecord(snap("a", { manual: false })).manual, false);
  assert.equal(L.cleanRecord(snap("a", { manual: "yes" })).manual, false);
  assert.equal(L.cleanRecord(snap("a")).manual, false);
});

test("control and bidi-override characters are removed like the Python loader does", () => {
  const r = L.cleanRecord(snap("a", { title: "Safe\u202etxt.exe\u0007 title\u2066x", source: "Goo\u0000gle\u061c", summary: "a\tb\nc" }));
  assert.equal(r.title, "Safetxt.exe titlex"); assert.equal(r.source, "Google"); assert.equal(r.summary, "a b c");
  assert.ok(L.cleanRecord(snap("a", { title: "family 👨\u200d👩" })).title.endsWith("\u200d👩"));
});
