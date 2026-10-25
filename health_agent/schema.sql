PRAGMA application_id = 1212633428;
PRAGMA user_version = 1;

CREATE TABLE patients (
    patient_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    is_synthetic INTEGER NOT NULL DEFAULT 1 CHECK (is_synthetic = 1)
);

CREATE TABLE health_records (
    record_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    indicator TEXT NOT NULL CHECK (indicator IN (
        'glucose', 'weight', 'systolic_blood_pressure', 'diastolic_blood_pressure')),
    value REAL NOT NULL CHECK (typeof(value) IN ('integer', 'real') AND value >= 0),
    unit TEXT NOT NULL,
    measured_at TEXT NOT NULL CHECK (
        length(measured_at) = 20 AND measured_at GLOB '????-??-??T??:??:??Z'
        AND julianday(measured_at) IS NOT NULL),
    measurement_context TEXT NOT NULL CHECK (
        measurement_context IN ('fasting', 'postprandial', 'random', 'unknown')),
    source TEXT NOT NULL,
    measurement_id TEXT,
    CHECK ((indicator = 'glucose' AND unit = 'mmol/L')
        OR (indicator = 'weight' AND unit = 'kg')
        OR (indicator IN ('systolic_blood_pressure', 'diastolic_blood_pressure') AND unit = 'mmHg')),
    CHECK (indicator = 'glucose' OR measurement_context = 'unknown'),
    CHECK ((indicator IN ('systolic_blood_pressure', 'diastolic_blood_pressure') AND measurement_id IS NOT NULL)
        OR (indicator IN ('glucose', 'weight') AND measurement_id IS NULL)),
    UNIQUE (patient_id, measurement_id, indicator)
);
CREATE INDEX health_lookup ON health_records(patient_id, indicator, measured_at DESC, record_id DESC);

CREATE TABLE medications (
    medication_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    medication_name TEXT NOT NULL,
    dose TEXT NOT NULL,
    frequency TEXT NOT NULL,
    start_date TEXT NOT NULL CHECK (length(start_date) = 10 AND date(start_date) IS NOT NULL),
    end_date TEXT CHECK (end_date IS NULL OR (length(end_date) = 10 AND date(end_date) IS NOT NULL AND end_date > start_date)),
    UNIQUE (patient_id, medication_id)
);

CREATE TABLE medication_events (
    event_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    event_date TEXT NOT NULL CHECK (length(event_date) = 10 AND date(event_date) IS NOT NULL),
    event_type TEXT NOT NULL CHECK (event_type IN ('start', 'stop', 'switch', 'dose_change')),
    old_medication_id TEXT,
    new_medication_id TEXT,
    FOREIGN KEY (patient_id, old_medication_id) REFERENCES medications(patient_id, medication_id),
    FOREIGN KEY (patient_id, new_medication_id) REFERENCES medications(patient_id, medication_id),
    CHECK ((event_type = 'start' AND old_medication_id IS NULL AND new_medication_id IS NOT NULL)
        OR (event_type = 'stop' AND old_medication_id IS NOT NULL AND new_medication_id IS NULL)
        OR (event_type IN ('switch', 'dose_change') AND old_medication_id IS NOT NULL
            AND new_medication_id IS NOT NULL AND old_medication_id <> new_medication_id))
);
CREATE INDEX event_lookup ON medication_events(patient_id, event_date);

CREATE TABLE reports (
    report_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    report_date TEXT NOT NULL CHECK (length(report_date) = 10 AND date(report_date) IS NOT NULL),
    upload_time TEXT NOT NULL CHECK (length(upload_time) = 20 AND julianday(upload_time) IS NOT NULL),
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    index_status TEXT NOT NULL DEFAULT 'pending' CHECK (index_status IN ('pending', 'ready', 'failed')),
    UNIQUE (patient_id, content_hash)
);
CREATE INDEX report_lookup ON reports(patient_id, report_date);

CREATE TABLE dataset_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
