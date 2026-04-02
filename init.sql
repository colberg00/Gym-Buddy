CREATE TABLE IF NOT EXISTS exercises (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sessions (
    id           SERIAL PRIMARY KEY,
    session_date DATE NOT NULL DEFAULT CURRENT_DATE,
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sets (
    id          SERIAL PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    exercise_id INTEGER NOT NULL REFERENCES exercises(id),
    set_number  SMALLINT NOT NULL,
    weight_kg   NUMERIC(6,2) NOT NULL,
    reps        SMALLINT NOT NULL,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS bodyweight (
    id          SERIAL PRIMARY KEY,
    weight_kg   NUMERIC(5,2) NOT NULL,
    measured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sets_exercise_id ON sets(exercise_id);
CREATE INDEX IF NOT EXISTS idx_sets_session_id  ON sets(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_date    ON sessions(session_date);
CREATE INDEX IF NOT EXISTS idx_bw_ts            ON bodyweight(measured_at);

CREATE OR REPLACE VIEW set_history AS
SELECT
    s.id           AS set_id,
    sess.id        AS session_id,
    sess.session_date,
    e.name         AS exercise,
    s.set_number,
    s.weight_kg,
    s.reps,
    CASE WHEN s.reps = 1 THEN s.weight_kg
         ELSE s.weight_kg * (1 + s.reps / 30.0)
    END            AS e1rm_kg,
    s.notes        AS set_notes,
    sess.notes     AS session_notes
FROM sets s
JOIN sessions sess ON sess.id = s.session_id
JOIN exercises e   ON e.id   = s.exercise_id;
