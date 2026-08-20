-- Run once against the D1 database (the Function also self-heals, this is
-- just so the table exists before the first request).
CREATE TABLE IF NOT EXISTS trip (
  id      INTEGER PRIMARY KEY,
  doc     TEXT    NOT NULL,
  version INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO trip (id, doc, version)
VALUES (1, '{"names":["OATT","POPP"],"rate":40,"items":[],"checks":{}}', 0);
