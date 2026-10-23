CREATE TABLE IF NOT EXISTS notes (
    id serial PRIMARY KEY,
    text text NOT NULL
);

INSERT INTO notes (text)
SELECT text FROM (VALUES ('welcome to notes'), ('every branch has its own copy')) AS seed(text)
WHERE NOT EXISTS (SELECT 1 FROM notes);
