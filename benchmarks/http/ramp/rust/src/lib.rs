use std::{collections::BTreeMap, env, path::Path, sync::Arc, thread, time::Instant};

use rusqlite::{Connection, OptionalExtension, params};
use serde::Serialize;
use serde_json::{Value, json};
use tokio::sync::{mpsc, oneshot};

pub const EXPERIMENT: &str = "sqlite-ramp-v2";
const CATEGORIES: [&str; 5] = ["books", "electronics", "home", "outdoors", "clothing"];

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Product {
    pub id: i64,
    pub name: String,
    pub category: String,
    pub price_cents: i64,
    pub stock: i64,
    pub revision: i64,
}
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Stock {
    pub id: i64,
    pub stock: i64,
    pub revision: i64,
}
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ListResponse {
    products: Vec<Product>,
    total: i64,
    offset: i64,
    limit: i64,
}
pub struct ResultBody {
    pub status: u16,
    pub body: Vec<u8>,
    pub service_ms: f64,
    pub db_ms: f64,
    pub timed: bool,
}

enum Command {
    Detail(i64, oneshot::Sender<Result<Option<Product>, String>>),
    List(i64, i64, oneshot::Sender<Result<Vec<Product>, String>>),
    Update(i64, i64, oneshot::Sender<Result<Option<Stock>, String>>),
    Integrity(oneshot::Sender<Result<(i64, i64, i64), String>>),
    Version(oneshot::Sender<Result<String, String>>),
    Pragmas(oneshot::Sender<Result<BTreeMap<String, Value>, String>>),
    CompileOptions(oneshot::Sender<Result<Vec<String>, String>>),
}

#[derive(Clone)]
pub struct Store {
    tx: mpsc::Sender<Command>,
    pub seed: i64,
}
fn err(status: u16, text: &str) -> ResultBody {
    ResultBody {
        status,
        body: bytes(json!({"error": text})),
        service_ms: 0.0,
        db_ms: 0.0,
        timed: false,
    }
}
fn bytes<T: Serialize>(v: T) -> Vec<u8> {
    serde_json::to_vec(&v).expect("serializable JSON")
}
fn product(id: i64) -> (String, String, i64, i64) {
    (
        format!("Product{id:05}"),
        CATEGORIES[((id - 1) % 5) as usize].to_owned(),
        500 + (id * 7919) % 50_000,
        (id * 37) % 201,
    )
}

pub fn setting(name: &str, default: &str) -> String {
    env::var(name).unwrap_or_else(|_| default.to_owned())
}
pub fn setting_int(name: &str, default: i64, min: i64, max: i64) -> i64 {
    let raw = setting(name, &default.to_string());
    let n = raw
        .parse()
        .unwrap_or_else(|_| panic!("{name} must be an integer"));
    assert!(
        (min..=max).contains(&n),
        "{name} must be between {min} and {max}"
    );
    n
}

impl Store {
    pub fn open(path: &str, seed: i64) -> Result<Self, String> {
        if let Some(parent) = Path::new(path).parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let connection = initialize(path, seed)?;
        let (tx, mut rx) = mpsc::channel(4096);
        thread::Builder::new()
            .name("sqlite-ramp-worker".into())
            .spawn(move || worker_loop(connection, &mut rx))
            .map_err(to_string)?;
        Ok(Self { tx, seed })
    }
    async fn call<T>(
        &self,
        command: impl FnOnce(oneshot::Sender<Result<T, String>>) -> Command,
    ) -> Result<T, String> {
        let (reply, receive) = oneshot::channel();
        self.tx
            .send(command(reply))
            .await
            .map_err(|_| "database worker stopped".to_owned())?;
        receive
            .await
            .map_err(|_| "database worker stopped".to_owned())?
    }
    pub async fn detail(&self, id: i64) -> ResultBody {
        let start = Instant::now();
        let db = Instant::now();
        let result = self.call(|r| Command::Detail(id, r)).await;
        let db_ms = db.elapsed().as_secs_f64() * 1000.0;
        match result {
            Ok(Some(p)) => timed(200, p, start, db_ms),
            Ok(None) => err(404, "product not found"),
            Err(_) => err(500, "database error"),
        }
    }
    pub async fn list(&self, offset: i64, limit: i64) -> ResultBody {
        let start = Instant::now();
        let db = Instant::now();
        let result = self.call(|r| Command::List(offset, limit, r)).await;
        let db_ms = db.elapsed().as_secs_f64() * 1000.0;
        match result {
            Ok(products) => timed(
                200,
                ListResponse {
                    products,
                    total: self.seed,
                    offset,
                    limit,
                },
                start,
                db_ms,
            ),
            Err(_) => err(500, "database error"),
        }
    }
    pub async fn update(&self, id: i64, delta: i64) -> ResultBody {
        let start = Instant::now();
        let db = Instant::now();
        let result = self.call(|r| Command::Update(id, delta, r)).await;
        let db_ms = db.elapsed().as_secs_f64() * 1000.0;
        match result {
            Ok(Some(value)) => timed(200, value, start, db_ms),
            Ok(None) => err(404, "product not found"),
            Err(_) => err(500, "database error"),
        }
    }
    pub async fn integrity(&self) -> ResultBody {
        match self.call(Command::Integrity).await {
            Ok((rows, stock, revisions)) => ResultBody {
                status: 200,
                body: bytes(json!({"rows":rows,"totalStock":stock,"totalRevisions":revisions})),
                service_ms: 0.0,
                db_ms: 0.0,
                timed: false,
            },
            Err(_) => err(500, "database error"),
        }
    }
    pub async fn info(&self, framework: &str) -> ResultBody {
        let version = self
            .call(Command::Version)
            .await
            .unwrap_or_else(|_| "unknown".into());
        let pragmas = self.call(Command::Pragmas).await.unwrap_or_default();
        let compile_options = self.call(Command::CompileOptions).await.unwrap_or_default();
        ResultBody {
            status: 200,
            body: bytes(
                json!({"experiment":EXPERIMENT,"runtime":"rust","framework":framework,"runtimeVersion":env!("RAMP_RUSTC_VERSION"),"frameworkVersion":framework_version(framework),"driver":"rusqlite","driverVersion":"0.40.2","sqliteVersion":version,"seedCount":self.seed,"workers":1,"pragmas":pragmas,"compileOptions":compile_options,"databaseWorker":"dedicated-thread"}),
            ),
            service_ms: 0.0,
            db_ms: 0.0,
            timed: false,
        }
    }
}
fn worker_loop(connection: Connection, rx: &mut mpsc::Receiver<Command>) {
    while let Some(command) = rx.blocking_recv() {
        match command {
            Command::Detail(id, reply) => {
                let _ = reply.send(query_detail(&connection, id));
            }
            Command::List(offset, limit, reply) => {
                let _ = reply.send(query_list(&connection, offset, limit));
            }
            Command::Update(id, delta, reply) => {
                let _ = reply.send(update_stock(&connection, id, delta));
            }
            Command::Integrity(reply) => {
                let _ = reply.send(query_integrity(&connection));
            }
            Command::Version(reply) => {
                let _ = reply.send(
                    connection
                        .query_row("SELECT sqlite_version()", [], |row| row.get(0))
                        .map_err(to_string),
                );
            }
            Command::Pragmas(reply) => {
                let _ = reply.send(actual_pragmas(&connection));
            }
            Command::CompileOptions(reply) => {
                let _ = reply.send(actual_compile_options(&connection));
            }
        }
    }
}
fn query_detail(connection: &Connection, id: i64) -> Result<Option<Product>, String> {
    let mut s = connection
        .prepare_cached(
            "SELECT id,name,category,price_cents,stock,revision FROM products WHERE id=?",
        )
        .map_err(to_string)?;
    s.query_row(params![id], row_product)
        .optional()
        .map_err(to_string)
}
fn query_list(connection: &Connection, offset: i64, limit: i64) -> Result<Vec<Product>, String> {
    let mut s=connection.prepare_cached("SELECT id,name,category,price_cents,stock,revision FROM products ORDER BY id LIMIT ? OFFSET ?").map_err(to_string)?;
    s.query_map(params![limit, offset], row_product)
        .map_err(to_string)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(to_string)
}
fn update_stock(connection: &Connection, id: i64, delta: i64) -> Result<Option<Stock>, String> {
    let mut s=connection.prepare_cached("UPDATE products SET stock=stock+?, revision=revision+1 WHERE id=? RETURNING id,stock,revision").map_err(to_string)?;
    s.query_row(params![delta, id], |r| {
        Ok(Stock {
            id: r.get(0)?,
            stock: r.get(1)?,
            revision: r.get(2)?,
        })
    })
    .optional()
    .map_err(to_string)
}
fn query_integrity(connection: &Connection) -> Result<(i64, i64, i64), String> {
    connection
        .query_row(
            "SELECT COUNT(*), COALESCE(SUM(stock),0), COALESCE(SUM(revision),0) FROM products",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .map_err(to_string)
}
fn actual_pragmas(connection: &Connection) -> Result<BTreeMap<String, Value>, String> {
    let mut values = BTreeMap::new();
    let journal_mode = connection
        .query_row("PRAGMA journal_mode", [], |row| row.get::<_, String>(0))
        .map_err(to_string)?;
    values.insert("journal_mode".to_owned(), json!(journal_mode));
    for (key, pragma) in [
        ("synchronous", "synchronous"),
        ("foreign_keys", "foreign_keys"),
        ("busy_timeout", "busy_timeout"),
        ("cache_size", "cache_size"),
        ("wal_autocheckpoint", "wal_autocheckpoint"),
        ("temp_store", "temp_store"),
    ] {
        let value = connection
            .query_row(&format!("PRAGMA {pragma}"), [], |row| row.get::<_, i64>(0))
            .map_err(to_string)?;
        values.insert(key.to_owned(), json!(value));
    }
    Ok(values)
}
fn actual_compile_options(connection: &Connection) -> Result<Vec<String>, String> {
    let mut statement = connection
        .prepare("PRAGMA compile_options")
        .map_err(to_string)?;
    statement
        .query_map([], |row| row.get(0))
        .map_err(to_string)?
        .collect::<Result<Vec<String>, _>>()
        .map_err(to_string)
}
fn framework_version(f: &str) -> &str {
    match f {
        "axum" => "0.8.9",
        "actix" => "4.15.0",
        "rocket" => "0.5.1",
        _ => "unknown",
    }
}
fn timed<T: Serialize>(status: u16, value: T, start: Instant, db_ms: f64) -> ResultBody {
    ResultBody {
        status,
        body: bytes(value),
        service_ms: start.elapsed().as_secs_f64() * 1000.0,
        db_ms,
        timed: true,
    }
}
fn row_product(r: &rusqlite::Row<'_>) -> rusqlite::Result<Product> {
    Ok(Product {
        id: r.get(0)?,
        name: r.get(1)?,
        category: r.get(2)?,
        price_cents: r.get(3)?,
        stock: r.get(4)?,
        revision: r.get(5)?,
    })
}
fn to_string<E: std::fmt::Display>(e: E) -> String {
    e.to_string()
}
fn initialize(path: &str, seed: i64) -> Result<Connection, String> {
    let mut c = Connection::open(path).map_err(to_string)?;
    for sql in [
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA foreign_keys=ON",
        "PRAGMA busy_timeout=5000",
        "PRAGMA cache_size=-2000",
        "PRAGMA wal_autocheckpoint=1000",
        "PRAGMA temp_store=MEMORY",
    ] {
        c.execute_batch(sql).map_err(to_string)?;
    }
    c.execute_batch("CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price_cents INTEGER NOT NULL, stock INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0)").map_err(to_string)?;
    let count: i64 = c
        .query_row("SELECT COUNT(*) FROM products", [], |r| r.get(0))
        .map_err(to_string)?;
    if count != 0 {
        if count != seed {
            return Err(format!(
                "existing products row count {count} does not match SEED_COUNT {seed}"
            ));
        };
        return Ok(c);
    };
    let tx = c.transaction().map_err(to_string)?;
    {
        let mut statement=tx.prepare("INSERT INTO products(id,name,category,price_cents,stock,revision) VALUES(?,?,?,?,?,0)").map_err(to_string)?;
        for id in 1..=seed {
            let (name, category, price, stock) = product(id);
            statement
                .execute(params![id, name, category, price, stock])
                .map_err(to_string)?;
        }
    }
    tx.commit().map_err(to_string)?;
    Ok(c)
}

pub fn valid_id(value: &str) -> Result<i64, ResultBody> {
    if value.starts_with('-') || value.starts_with('+') {
        return Err(err(400, "id must be a positive integer"));
    };
    let id = value
        .parse::<i64>()
        .map_err(|_| err(400, "id must be a positive integer"))?;
    if !(1..=9_007_199_254_740_991).contains(&id) {
        return Err(err(400, "id must be a positive integer"));
    };
    Ok(id)
}
pub fn list_query(raw: Option<&str>, seed: i64) -> Result<(i64, i64), ResultBody> {
    let mut fields = BTreeMap::new();
    if let Some(raw) = raw.filter(|v| !v.is_empty()) {
        for part in raw.split('&') {
            let Some((key, value)) = part.split_once('=') else {
                return Err(err(400, "invalid query parameters"));
            };
            if !matches!(key, "offset" | "limit") || fields.insert(key, value).is_some() {
                return Err(err(400, "invalid query parameters"));
            }
        }
    };
    let offset = parse_range(
        fields.get("offset").copied().unwrap_or("0"),
        0,
        seed,
        "offset",
    )?;
    let limit = parse_range(
        fields.get("limit").copied().unwrap_or("20"),
        1,
        100,
        "limit",
    )?;
    Ok((offset, limit))
}
fn parse_range(text: &str, min: i64, max: i64, name: &str) -> Result<i64, ResultBody> {
    let value = text
        .parse()
        .map_err(|_| err(400, &format!("{name} is out of range")))?;
    if value < min || value > max {
        return Err(err(400, &format!("{name} is out of range")));
    };
    Ok(value)
}
pub fn stock_body(content_type: Option<&str>, body: &[u8]) -> Result<i64, ResultBody> {
    if body.len() > 65_536 {
        return Err(err(413, "body exceeds 65536 bytes"));
    };
    if !content_type
        .and_then(|v| v.split(';').next())
        .is_some_and(|v| v.trim().eq_ignore_ascii_case("application/json"))
    {
        return Err(err(415, "content type must be application/json"));
    };
    let value: Value = serde_json::from_slice(body)
        .map_err(|_| err(400, "body must be exactly {delta: integer}"))?;
    let Some(object) = value.as_object() else {
        return Err(err(400, "body must be exactly {delta: integer}"));
    };
    if object.len() != 1 {
        return Err(err(400, "body must be exactly {delta: integer}"));
    };
    let Some(delta) = object.get("delta").and_then(Value::as_i64) else {
        return Err(err(400, "delta must be an integer between -100 and 100"));
    };
    if !(-100..=100).contains(&delta) {
        return Err(err(400, "delta must be an integer between -100 and 100"));
    };
    Ok(delta)
}
pub fn header(result: &ResultBody) -> Option<String> {
    result.timed.then(|| {
        format!(
            "service;dur={:.3}, db;dur={:.3}",
            result.service_ms, result.db_ms
        )
    })
}
pub fn shared_store(store: Store) -> Arc<Store> {
    Arc::new(store)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn seed_and_atomic_updates_are_preserved() {
        let path = format!(
            "{}/ramp-rust-{}.sqlite",
            std::env::temp_dir().display(),
            std::process::id()
        );
        let _ = std::fs::remove_file(&path);
        let store = Store::open(&path, 100).expect("store starts");
        assert_eq!(store.update(1, 1).await.status, 200);
        assert_eq!(store.update(1, 1).await.status, 200);
        let integrity: serde_json::Value =
            serde_json::from_slice(&store.integrity().await.body).unwrap();
        assert_eq!(integrity["rows"], 100);
        assert_eq!(integrity["totalRevisions"], 2);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn strict_input_rejects_invalid_values() {
        assert!(valid_id("-1").is_err());
        assert!(list_query(Some("limit=1&limit=2"), 100).is_err());
        assert!(stock_body(Some("application/json"), br#"{"delta":true}"#).is_err());
    }
}
