CREATE TABLE IF NOT EXISTS items (
    id serial PRIMARY KEY,
    name text NOT NULL
);

INSERT INTO items (name)
SELECT name FROM (VALUES ('keyboard'), ('mouse'), ('monitor')) AS seed(name)
WHERE NOT EXISTS (SELECT 1 FROM items);
