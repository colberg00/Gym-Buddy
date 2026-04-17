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
    weight      NUMERIC(6,2) NOT NULL,
    reps        SMALLINT NOT NULL,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS bodyweight (
    id          SERIAL PRIMARY KEY,
    weight      NUMERIC(5,2) NOT NULL,
    measured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS workout_templates (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    sort_order SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS template_exercises (
    id              SERIAL PRIMARY KEY,
    template_id     INTEGER NOT NULL REFERENCES workout_templates(id) ON DELETE CASCADE,
    exercise_id     INTEGER NOT NULL REFERENCES exercises(id),
    position        SMALLINT NOT NULL DEFAULT 0,
    default_sets    SMALLINT NOT NULL DEFAULT 3,
    target_reps_min SMALLINT NOT NULL DEFAULT 6,
    target_reps_max SMALLINT NOT NULL DEFAULT 8,
    UNIQUE (template_id, exercise_id)
);

CREATE INDEX IF NOT EXISTS idx_sets_exercise_id ON sets(exercise_id);
CREATE INDEX IF NOT EXISTS idx_sets_session_id  ON sets(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_date    ON sessions(session_date);
CREATE INDEX IF NOT EXISTS idx_bw_ts            ON bodyweight(measured_at);
CREATE INDEX IF NOT EXISTS idx_tmpl_ex          ON template_exercises(template_id);

CREATE OR REPLACE VIEW set_history AS
SELECT
    s.id           AS set_id,
    sess.id        AS session_id,
    sess.session_date,
    e.name         AS exercise,
    s.set_number,
    s.weight,
    s.reps,
    CASE WHEN s.reps = 1 THEN s.weight
         ELSE s.weight * (1 + s.reps / 30.0)
    END            AS e1rm,
    s.notes        AS set_notes,
    sess.notes     AS session_notes
FROM sets s
JOIN sessions sess ON sess.id = s.session_id
JOIN exercises e   ON e.id   = s.exercise_id;

-- Seed workout templates and exercises
DO $$
DECLARE
    upper_id INT;
    lower_id INT;
BEGIN
    INSERT INTO workout_templates (name, sort_order) VALUES ('Upper', 1), ('Lower', 2)
    ON CONFLICT (name) DO NOTHING;

    SELECT id INTO upper_id FROM workout_templates WHERE name = 'Upper';
    SELECT id INTO lower_id FROM workout_templates WHERE name = 'Lower';

    INSERT INTO exercises (name) VALUES
        ('Barbell Incline Bench Press'),
        ('Pec Deck Fly'),
        ('Weighted Pull-Up'),
        ('Dumbbell Lateral Raise'),
        ('Pendlay Row'),
        ('Cable Overhead Rope Tricep Extension'),
        ('Bayesian Curl'),
        ('Leg Curl'),
        ('Pendulum Squat'),
        ('Romanian Deadlift'),
        ('Leg Extension'),
        ('Hip Abduction'),
        ('Standing Calf Raise')
    ON CONFLICT (name) DO NOTHING;

    INSERT INTO template_exercises (template_id, exercise_id, position, default_sets, target_reps_min, target_reps_max)
    VALUES
        (upper_id, (SELECT id FROM exercises WHERE name = 'Barbell Incline Bench Press'), 1, 3, 6, 8),
        (upper_id, (SELECT id FROM exercises WHERE name = 'Pec Deck Fly'),                2, 2, 10, 12),
        (upper_id, (SELECT id FROM exercises WHERE name = 'Weighted Pull-Up'),             3, 3, 6, 8),
        (upper_id, (SELECT id FROM exercises WHERE name = 'Dumbbell Lateral Raise'),       4, 2, 8, 10),
        (upper_id, (SELECT id FROM exercises WHERE name = 'Pendlay Row'),                  5, 2, 8, 10),
        (upper_id, (SELECT id FROM exercises WHERE name = 'Cable Overhead Rope Tricep Extension'), 6, 2, 8, 10),
        (upper_id, (SELECT id FROM exercises WHERE name = 'Bayesian Curl'),                7, 2, 8, 10)
    ON CONFLICT (template_id, exercise_id) DO NOTHING;

    INSERT INTO template_exercises (template_id, exercise_id, position, default_sets, target_reps_min, target_reps_max)
    VALUES
        (lower_id, (SELECT id FROM exercises WHERE name = 'Leg Curl'),            1, 2, 6, 8),
        (lower_id, (SELECT id FROM exercises WHERE name = 'Pendulum Squat'),       2, 3, 6, 8),
        (lower_id, (SELECT id FROM exercises WHERE name = 'Romanian Deadlift'),    3, 3, 6, 8),
        (lower_id, (SELECT id FROM exercises WHERE name = 'Leg Extension'),        4, 2, 8, 10),
        (lower_id, (SELECT id FROM exercises WHERE name = 'Hip Abduction'),        5, 2, 8, 10),
        (lower_id, (SELECT id FROM exercises WHERE name = 'Standing Calf Raise'),  6, 3, 8, 10)
    ON CONFLICT (template_id, exercise_id) DO NOTHING;
END $$;
