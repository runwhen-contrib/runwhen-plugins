-- pg-locks fixtures: a small table in `orders` for the rw-fixture-locks sessions to lock.
-- Idempotent. up.sh runs every scenarios/*.sql as the superuser on the primary.
\connect orders
SET client_min_messages = warning;
CREATE TABLE IF NOT EXISTS lock_fixture (id int PRIMARY KEY, note text NOT NULL);
INSERT INTO lock_fixture (id, note) VALUES (1, 'a'), (2, 'b') ON CONFLICT DO NOTHING;
