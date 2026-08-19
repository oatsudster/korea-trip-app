/**
 * Shared trip document, stored as one row in D1.
 *
 * GET  /api/state            -> { ok, doc, version }
 * PUT  /api/state {doc,version} -> 200 { ok, doc, version }
 *                            -> 409 { ok:false, conflict, doc, version }  (someone wrote first)
 *
 * Writes are compare-and-set on `version`, so two phones saving at the same
 * moment can never silently lose one of the entries: the loser gets 409 with
 * the winning document and the client replays its own change on top.
 *
 * If the D1 binding is missing the endpoint answers 501 and the app falls
 * back to this-device-only storage instead of breaking.
 */

const EMPTY = { names: ['เรา', 'แฟน'], rate: 40, items: [] };
const MAX_BYTES = 256 * 1024;

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
    },
  });

async function ensure(db) {
  await db.batch([
    db.prepare(
      'CREATE TABLE IF NOT EXISTS trip (id INTEGER PRIMARY KEY, doc TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0)'
    ),
    db.prepare('INSERT OR IGNORE INTO trip (id, doc, version) VALUES (1, ?, 0)').bind(
      JSON.stringify(EMPTY)
    ),
  ]);
}

async function load(db) {
  const row = await db.prepare('SELECT doc, version FROM trip WHERE id = 1').first();
  if (!row) return { doc: EMPTY, version: 0 };
  let doc;
  try {
    doc = JSON.parse(row.doc);
  } catch {
    doc = EMPTY;
  }
  return { doc, version: row.version | 0 };
}

function valid(doc) {
  return (
    doc &&
    typeof doc === 'object' &&
    Array.isArray(doc.items) &&
    Array.isArray(doc.names) &&
    doc.names.length >= 2 &&
    typeof doc.rate === 'number' &&
    isFinite(doc.rate) &&
    doc.rate > 0
  );
}

export async function onRequestGet({ env }) {
  if (!env.DB) return json({ ok: false, error: 'no D1 binding named DB' }, 501);
  try {
    await ensure(env.DB);
    const cur = await load(env.DB);
    return json({ ok: true, ...cur });
  } catch (e) {
    return json({ ok: false, error: String(e) }, 500);
  }
}

export async function onRequestPut({ request, env }) {
  if (!env.DB) return json({ ok: false, error: 'no D1 binding named DB' }, 501);
  try {
    const body = await request.json();
    const doc = body && body.doc;
    const base = Number(body && body.version) || 0;

    if (!valid(doc)) return json({ ok: false, error: 'bad document' }, 400);

    const text = JSON.stringify(doc);
    if (text.length > MAX_BYTES) return json({ ok: false, error: 'document too large' }, 413);

    await ensure(env.DB);
    const res = await env.DB.prepare(
      'UPDATE trip SET doc = ?, version = version + 1 WHERE id = 1 AND version = ?'
    )
      .bind(text, base)
      .run();

    const changed = res && res.meta ? res.meta.changes : 0;
    const cur = await load(env.DB);

    if (!changed) return json({ ok: false, conflict: true, ...cur }, 409);
    return json({ ok: true, ...cur });
  } catch (e) {
    return json({ ok: false, error: String(e) }, 500);
  }
}
