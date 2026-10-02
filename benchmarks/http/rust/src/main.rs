use std::{
    collections::{BTreeMap, HashMap},
    env, fs,
    net::SocketAddr,
    path::Path,
    sync::{Arc, Mutex},
    thread,
};

use axum::{
    Json, Router,
    body::to_bytes,
    extract::{Path as AxumPath, Request, State},
    http::{HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::sync::{mpsc, oneshot};

const CATEGORIES: [&str; 5] = ["books", "electronics", "home", "sports", "toys"];
const EVENT_TYPES: [&str; 3] = ["view", "click", "purchase"];
const MAX_BODY_BYTES: usize = 1 << 20;
const SCHEMA_VERSION: &str = "1";

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
struct Product {
    id: i64,
    name: String,
    category: String,
    price_cents: i64,
    stock: i64,
    tags: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct CategoryReport {
    category: String,
    count: i64,
    stock: i64,
    inventory_value_cents: i64,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct QuoteRequest {
    items: Vec<QuoteLine>,
    coupon: Option<Option<String>>,
}
#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct QuoteLine {
    product_id: Option<i64>,
    quantity: Option<i64>,
}
#[derive(Clone, Debug, Deserialize)]
struct EventRequest {
    events: Vec<Event>,
}
#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Event {
    user_id: Option<i64>,
    r#type: String,
    value: Option<i64>,
}

fn product_for(id: i64) -> Product {
    let category = CATEGORIES[((id - 1) % 5) as usize].to_owned();
    Product {
        id,
        name: format!("Product {id:05}"),
        category: category.clone(),
        price_cents: 500 + (id * 7919) % 50_000,
        stock: (id * 37) % 201,
        tags: vec![
            category,
            if id % 3 == 0 { "featured" } else { "standard" }.to_owned(),
            if id % 2 == 0 { "even" } else { "odd" }.to_owned(),
        ],
    }
}

type EventTotals = BTreeMap<String, (i64, i64)>;
type EventCounters = BTreeMap<(i64, String), (i64, i64)>;
fn empty_events() -> EventTotals {
    EVENT_TYPES
        .into_iter()
        .map(|kind| (kind.to_owned(), (0, 0)))
        .collect()
}

enum DbCommand {
    Product(i64, oneshot::Sender<Result<Option<Product>, String>>),
    List(
        String,
        String,
        i64,
        i64,
        oneshot::Sender<Result<(Vec<Product>, i64), String>>,
    ),
    Catalog(oneshot::Sender<Result<Vec<CategoryReport>, String>>),
    Events(oneshot::Sender<Result<EventTotals, String>>),
    AddEvents(Vec<Event>, oneshot::Sender<Result<(), String>>),
}

#[derive(Clone)]
enum Backend {
    Sqlite(mpsc::Sender<DbCommand>),
    Memory {
        products: Arc<Vec<Product>>,
        events: Arc<Mutex<EventCounters>>,
    },
}

impl Backend {
    async fn product(&self, id: i64) -> Result<Option<Product>, String> {
        match self {
            Self::Memory { products, .. } => Ok(id
                .checked_sub(1)
                .and_then(|index| products.get(usize::try_from(index).ok()?))
                .cloned()),
            Self::Sqlite(tx) => call(tx, |reply| DbCommand::Product(id, reply)).await,
        }
    }
    async fn list(
        &self,
        category: String,
        query: String,
        limit: i64,
        offset: i64,
    ) -> Result<(Vec<Product>, i64), String> {
        match self {
            Self::Memory { products, .. } => {
                let filtered: Vec<&Product> = products
                    .iter()
                    .filter(|p| {
                        (category.is_empty() || p.category == category)
                            && (query.is_empty() || p.name.to_lowercase().contains(&query))
                    })
                    .collect();
                let total =
                    i64::try_from(filtered.len()).map_err(|_| "too many products".to_owned())?;
                Ok((
                    filtered
                        .into_iter()
                        .skip(usize::try_from(offset).unwrap_or(usize::MAX))
                        .take(usize::try_from(limit).unwrap_or(0))
                        .cloned()
                        .collect(),
                    total,
                ))
            }
            Self::Sqlite(tx) => {
                call(tx, |reply| {
                    DbCommand::List(category, query, limit, offset, reply)
                })
                .await
            }
        }
    }
    async fn catalog(&self) -> Result<Vec<CategoryReport>, String> {
        match self {
            Self::Memory { products, .. } => Ok(catalog_memory(products)),
            Self::Sqlite(tx) => call(tx, DbCommand::Catalog).await,
        }
    }
    async fn events(&self) -> Result<EventTotals, String> {
        match self {
            Self::Memory { events, .. } => events
                .lock()
                .map(|items| {
                    let mut totals = empty_events();
                    for ((_, kind), (count, value)) in items.iter() {
                        let entry = totals.entry(kind.clone()).or_insert((0, 0));
                        entry.0 += count;
                        entry.1 += value;
                    }
                    totals
                })
                .map_err(|_| "event counter lock poisoned".to_owned()),
            Self::Sqlite(tx) => call(tx, DbCommand::Events).await,
        }
    }
    async fn add_events(&self, items: Vec<Event>) -> Result<(), String> {
        match self {
            Self::Memory { events, .. } => {
                let mut totals = events
                    .lock()
                    .map_err(|_| "event counter lock poisoned".to_owned())?;
                for item in items {
                    let entry = totals
                        .entry((item.user_id.unwrap_or_default(), item.r#type))
                        .or_insert((0, 0));
                    entry.0 += 1;
                    entry.1 += item.value.unwrap_or_default();
                }
                Ok(())
            }
            Self::Sqlite(tx) => call(tx, |reply| DbCommand::AddEvents(items, reply)).await,
        }
    }
}

async fn call<T>(
    tx: &mpsc::Sender<DbCommand>,
    make: impl FnOnce(oneshot::Sender<Result<T, String>>) -> DbCommand,
) -> Result<T, String> {
    let (reply, receive) = oneshot::channel();
    tx.send(make(reply))
        .await
        .map_err(|_| "database worker stopped".to_owned())?;
    receive
        .await
        .map_err(|_| "database worker stopped".to_owned())?
}

#[derive(Clone)]
struct AppState {
    backend: Backend,
    seed_count: i64,
}

fn response(status: StatusCode, body: Value) -> Response {
    (
        status,
        [(
            header::CONTENT_TYPE,
            HeaderValue::from_static("application/json"),
        )],
        Json(body),
    )
        .into_response()
}
fn error(status: StatusCode, message: &str) -> Response {
    response(status, json!({"error": message}))
}
fn database_error(_error: String) -> Response {
    error(StatusCode::INTERNAL_SERVER_ERROR, "database error")
}
fn method_not_allowed(allow: &str) -> Response {
    (
        StatusCode::METHOD_NOT_ALLOWED,
        [
            (
                header::ALLOW,
                HeaderValue::from_str(allow).expect("static allow"),
            ),
            (
                header::CONTENT_TYPE,
                HeaderValue::from_static("application/json"),
            ),
        ],
        Json(json!({"error":"method not allowed"})),
    )
        .into_response()
}
async fn method_not_allowed_for_path(request: Request) -> Response {
    let allow = match request.uri().path() {
        "/cart/quote" | "/events/batch" => "POST",
        _ => "GET, HEAD",
    };
    method_not_allowed(allow)
}

async fn healthz() -> Response {
    response(StatusCode::OK, json!({"status":"ok"}))
}

async fn product_detail(
    State(state): State<AppState>,
    AxumPath(raw_id): AxumPath<String>,
) -> Response {
    let Some(id) = decimal(&raw_id, 1, 1_000_000_000) else {
        return error(StatusCode::BAD_REQUEST, "id must be a positive integer");
    };
    match state.backend.product(id).await {
        Ok(Some(product)) => response(StatusCode::OK, json!(product)),
        Ok(None) => error(StatusCode::NOT_FOUND, "product not found"),
        Err(e) => database_error(e),
    }
}

async fn empty_product_id() -> Response {
    error(StatusCode::BAD_REQUEST, "id must be a positive integer")
}

async fn list_products(State(state): State<AppState>, request: Request) -> Response {
    let query = request.uri().query().unwrap_or("");
    let parameters: HashMap<String, String> = url_form(query);
    let category = parameters.get("category").cloned().unwrap_or_default();
    if !category.is_empty() && !CATEGORIES.contains(&category.as_str()) {
        return error(StatusCode::BAD_REQUEST, "category is invalid");
    }
    let search = parameters
        .get("q")
        .map_or_else(String::new, |s| s.to_lowercase());
    let Some(mut offset) = optional_decimal(parameters.get("offset"), 0, 1_000_000_000) else {
        return error(StatusCode::BAD_REQUEST, "offset is invalid");
    };
    let Some(limit) = optional_decimal(parameters.get("limit"), 20, 100) else {
        return error(StatusCode::BAD_REQUEST, "limit is invalid");
    };
    if limit == 0 {
        return error(StatusCode::BAD_REQUEST, "limit is invalid");
    }
    match state.backend.list(category, search, limit, offset).await {
        Ok((products, total)) => {
            offset = offset.min(total); // list again if the offset was clamped
            let products = if offset == total {
                Vec::new()
            } else {
                products
            };
            response(
                StatusCode::OK,
                json!({"products":products,"total":total,"offset":offset,"limit":limit}),
            )
        }
        Err(e) => database_error(e),
    }
}

async fn catalog_report(State(state): State<AppState>) -> Response {
    match state.backend.catalog().await {
        Ok(rows) => {
            let total_stock: i64 = rows.iter().map(|x| x.stock).sum();
            let total_inventory_value_cents: i64 =
                rows.iter().map(|x| x.inventory_value_cents).sum();
            response(
                StatusCode::OK,
                json!({"categories":rows,"totalStock":total_stock,"totalInventoryValueCents":total_inventory_value_cents}),
            )
        }
        Err(e) => database_error(e),
    }
}

async fn events_report(State(state): State<AppState>) -> Response {
    match state.backend.events().await {
        Ok(totals) => response(StatusCode::OK, totals_json(&totals)),
        Err(e) => database_error(e),
    }
}

async fn quote(State(state): State<AppState>, request: Request) -> Response {
    let input = match parse_json::<QuoteRequest>(request).await {
        Ok(input) => input,
        Err(reason) => return input_error(reason, "invalid quote request"),
    };
    if input.items.is_empty() || input.items.len() > 100 {
        return error(StatusCode::BAD_REQUEST, "invalid quote request");
    }
    let coupon = match input.coupon {
        Some(Some(value)) if value != "SAVE10" => {
            return error(StatusCode::BAD_REQUEST, "coupon is invalid");
        }
        Some(Some(_)) => true,
        _ => false,
    };
    let mut requested = HashMap::new();
    let mut lines = Vec::new();
    let mut subtotal = 0_i64;
    for line in input.items {
        let (Some(id), Some(quantity)) = (line.product_id, line.quantity) else {
            return error(
                StatusCode::BAD_REQUEST,
                "quantity is invalid or unavailable",
            );
        };
        if !(1..=1_000_000_000).contains(&id) || !(1..=100).contains(&quantity) {
            return error(
                StatusCode::BAD_REQUEST,
                "quantity is invalid or unavailable",
            );
        };
        match state.backend.product(id).await {
            Ok(Some(product)) => {
                let total_quantity = requested.entry(id).or_insert(0_i64);
                *total_quantity += quantity;
                if *total_quantity > product.stock {
                    return error(
                        StatusCode::BAD_REQUEST,
                        "quantity is invalid or unavailable",
                    );
                };
                let line_total = product.price_cents * quantity;
                subtotal += line_total;
                lines.push(json!({"productId":id,"quantity":quantity,"unitPriceCents":product.price_cents,"lineTotalCents":line_total}));
            }
            Ok(None) => return error(StatusCode::NOT_FOUND, "product not found"),
            Err(e) => return database_error(e),
        }
    }
    let discount = if coupon { subtotal * 10 / 100 } else { 0 };
    let tax = (subtotal - discount) * 20 / 100;
    response(
        StatusCode::OK,
        json!({"items":lines,"subtotalCents":subtotal,"discountCents":discount,"taxCents":tax,"totalCents":subtotal-discount+tax}),
    )
}

async fn batch_events(State(state): State<AppState>, request: Request) -> Response {
    let input = match parse_json::<EventRequest>(request).await {
        Ok(input) => input,
        Err(reason) => return input_error(reason, "invalid events request"),
    };
    if input.events.is_empty() || input.events.len() > 100 {
        return error(StatusCode::BAD_REQUEST, "invalid events request");
    }
    let mut counts = empty_events();
    let mut canonical = String::new();
    for item in &input.events {
        let (Some(user_id), Some(value)) = (item.user_id, item.value) else {
            return error(StatusCode::BAD_REQUEST, "event is invalid");
        };
        if !(1..=state.seed_count).contains(&user_id)
            || !(0..=1_000_000).contains(&value)
            || !EVENT_TYPES.contains(&item.r#type.as_str())
        {
            return error(StatusCode::BAD_REQUEST, "event is invalid");
        };
        let entry = counts.entry(item.r#type.clone()).or_insert((0, 0));
        entry.0 += 1;
        entry.1 += value;
        canonical.push_str(&format!("{user_id}:{}:{value}\n", item.r#type));
    }
    if let Err(e) = state.backend.add_events(input.events).await {
        return database_error(e);
    }
    let hash = format!("{:x}", Sha256::digest(canonical.as_bytes()));
    response(
        StatusCode::OK,
        json!({"counts":counts.iter().map(|(key,(count,_))|(key,count)).collect::<BTreeMap<_,_>>(),"values":counts.iter().map(|(key,(_,value))|(key,value)).collect::<BTreeMap<_,_>>(),"sha256":hash}),
    )
}

#[derive(Debug)]
enum ParseError {
    Bad,
    TooLarge,
    MediaType,
}
async fn parse_json<T: for<'a> Deserialize<'a>>(request: Request) -> Result<T, ParseError> {
    if !request
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| {
            v.split(';')
                .next()
                .is_some_and(|x| x.trim().eq_ignore_ascii_case("application/json"))
        })
    {
        return Err(ParseError::MediaType);
    }
    let body = to_bytes(request.into_body(), MAX_BODY_BYTES + 1)
        .await
        .map_err(|_| ParseError::TooLarge)?;
    if body.len() > MAX_BODY_BYTES {
        return Err(ParseError::TooLarge);
    }
    serde_json::from_slice(&body).map_err(|_| ParseError::Bad)
}
fn input_error(error_kind: ParseError, message: &str) -> Response {
    match error_kind {
        ParseError::MediaType => error(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "content-type must be application/json",
        ),
        ParseError::TooLarge => error(StatusCode::PAYLOAD_TOO_LARGE, "request body too large"),
        ParseError::Bad => error(StatusCode::BAD_REQUEST, message),
    }
}
fn decimal(raw: &str, minimum: i64, maximum: i64) -> Option<i64> {
    (!raw.is_empty() && raw.bytes().all(|item| item.is_ascii_digit()))
        .then(|| raw.parse::<i64>().ok())
        .flatten()
        .filter(|value| (*value >= minimum) && (*value <= maximum))
}
fn optional_decimal(raw: Option<&String>, fallback: i64, maximum: i64) -> Option<i64> {
    raw.map_or(Some(fallback), |item| {
        if item.is_empty() {
            Some(fallback)
        } else {
            decimal(item, 0, maximum)
        }
    })
}
fn url_form(query: &str) -> HashMap<String, String> {
    query
        .split('&')
        .filter_map(|piece| {
            piece
                .split_once('=')
                .or(Some((piece, "")))
                .map(|(k, v)| (percent_decode(k), percent_decode(v)))
        })
        .collect()
}
fn percent_decode(raw: &str) -> String {
    let mut bytes = Vec::new();
    let mut chars = raw.as_bytes().iter().copied();
    while let Some(c) = chars.next() {
        if c == b'%' {
            let a = chars.next();
            let b = chars.next();
            if let (Some(a), Some(b)) = (a, b)
                && let Ok(value) = u8::from_str_radix(&format!("{}{}", a as char, b as char), 16)
            {
                bytes.push(value);
                continue;
            }
        }
        bytes.push(if c == b'+' { b' ' } else { c });
    }
    String::from_utf8_lossy(&bytes).into_owned()
}
fn totals_json(totals: &EventTotals) -> Value {
    json!({"counts":totals.iter().map(|(key,(count,_))|(key,count)).collect::<BTreeMap<_,_>>(),"values":totals.iter().map(|(key,(_,value))|(key,value)).collect::<BTreeMap<_,_>>()})
}
fn catalog_memory(products: &[Product]) -> Vec<CategoryReport> {
    CATEGORIES
        .into_iter()
        .map(|category| {
            let mut result = CategoryReport {
                category: category.to_owned(),
                count: 0,
                stock: 0,
                inventory_value_cents: 0,
            };
            for product in products.iter().filter(|item| item.category == category) {
                result.count += 1;
                result.stock += product.stock;
                result.inventory_value_cents += product.stock * product.price_cents;
            }
            result
        })
        .collect()
}

fn sqlite_backend(
    path: &str,
    seed_count: i64,
) -> Result<(Backend, String, thread::JoinHandle<()>), String> {
    if path == ":memory:" {
        return Err("DB_PATH must name a filesystem database".to_owned());
    }
    if let Some(parent) = Path::new(path).parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?
    }
    let mut connection = Connection::open(path).map_err(|e| e.to_string())?;
    configure(&connection)?;
    initialize(&mut connection, seed_count)?;
    let version: String = connection
        .query_row("SELECT sqlite_version()", [], |row| row.get(0))
        .map_err(|e| e.to_string())?;
    let (tx, mut rx) = mpsc::channel(256);
    let thread = std::thread::Builder::new()
        .name("sqlite-worker".to_owned())
        .spawn(move || {
            while let Some(command) = rx.blocking_recv() {
                run_db_command(&mut connection, command);
            }
        })
        .map_err(|e| e.to_string())?;
    Ok((Backend::Sqlite(tx), version, thread))
}
fn configure(connection: &Connection) -> Result<(), String> {
    for query in [
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA foreign_keys=ON",
        "PRAGMA busy_timeout=5000",
        "PRAGMA cache_size=-2000",
        "PRAGMA wal_autocheckpoint=1000",
        "PRAGMA temp_store=MEMORY",
    ] {
        connection.execute_batch(query).map_err(|e| e.to_string())?;
    }
    let journal: String = connection
        .query_row("PRAGMA journal_mode", [], |row| row.get(0))
        .map_err(|e| e.to_string())?;
    if !journal.eq_ignore_ascii_case("wal") {
        return Err("WAL mode was not enabled".to_owned());
    }
    Ok(())
}
fn initialize(connection: &mut Connection, seed_count: i64) -> Result<(), String> {
    let metadata_exists: Option<String> = connection
        .query_row(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(|error| error.to_string())?;
    if metadata_exists.is_some() {
        let version: String = connection
            .query_row(
                "SELECT value FROM metadata WHERE key='schema_version'",
                [],
                |row| row.get(0),
            )
            .map_err(|error| error.to_string())?;
        let stored: String = connection
            .query_row(
                "SELECT value FROM metadata WHERE key='seed_count'",
                [],
                |row| row.get(0),
            )
            .map_err(|e| e.to_string())?;
        if version != SCHEMA_VERSION || stored != seed_count.to_string() {
            return Err(format!(
                "database metadata does not match schema version {SCHEMA_VERSION} and SEED_COUNT {seed_count}"
            ));
        }
        return Ok(());
    }
    let tx = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|e| e.to_string())?;
    tx.execute_batch("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL); CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price_cents INTEGER NOT NULL, stock INTEGER NOT NULL, tags TEXT NOT NULL); CREATE INDEX products_category_id ON products(category, id); CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL); CREATE TABLE event_totals(user_id INTEGER NOT NULL REFERENCES users(id), type TEXT NOT NULL, count INTEGER NOT NULL CHECK(count >= 0), value_total INTEGER NOT NULL CHECK(value_total >= 0), PRIMARY KEY(user_id, type));").map_err(|e|e.to_string())?;
    {
        let mut products = tx
            .prepare(
                "INSERT INTO products(id,name,category,price_cents,stock,tags) VALUES(?,?,?,?,?,?)",
            )
            .map_err(|e| e.to_string())?;
        let mut users = tx
            .prepare("INSERT INTO users(id,name) VALUES(?,?)")
            .map_err(|e| e.to_string())?;
        for id in 1..=seed_count {
            let item = product_for(id);
            products
                .execute(params![
                    item.id,
                    item.name,
                    item.category,
                    item.price_cents,
                    item.stock,
                    serde_json::to_string(&item.tags).map_err(|e| e.to_string())?
                ])
                .map_err(|e| e.to_string())?;
            users
                .execute(params![id, format!("User {id:05}")])
                .map_err(|e| e.to_string())?;
        }
    }
    tx.execute(
        "INSERT INTO metadata(key,value) VALUES('schema_version',?),('seed_count',?)",
        params![SCHEMA_VERSION, seed_count.to_string()],
    )
    .map_err(|e| e.to_string())?;
    tx.commit().map_err(|e| e.to_string())
}
fn product_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Product> {
    let tags: String = row.get(5)?;
    Ok(Product {
        id: row.get(0)?,
        name: row.get(1)?,
        category: row.get(2)?,
        price_cents: row.get(3)?,
        stock: row.get(4)?,
        tags: serde_json::from_str(&tags).map_err(|e| {
            rusqlite::Error::FromSqlConversionFailure(5, rusqlite::types::Type::Text, Box::new(e))
        })?,
    })
}
fn run_db_command(connection: &mut Connection, command: DbCommand) {
    match command {
        DbCommand::Product(id, reply) => {
            let _ = reply.send(
                connection
                    .query_row(
                        "SELECT id,name,category,price_cents,stock,tags FROM products WHERE id=?",
                        params![id],
                        product_row,
                    )
                    .optional()
                    .map_err(|e| e.to_string()),
            );
        }
        DbCommand::List(category, query, limit, offset, reply) => {
            let result = (|| {
                let total=connection.query_row("SELECT COUNT(*) FROM products WHERE (?='' OR category=?) AND (?='' OR instr(lower(name),lower(?))>0)",params![category,category,query,query],|row|row.get(0)).map_err(|e|e.to_string())?;
                let mut statement=connection.prepare("SELECT id,name,category,price_cents,stock,tags FROM products WHERE (?='' OR category=?) AND (?='' OR instr(lower(name),lower(?))>0) ORDER BY id LIMIT ? OFFSET ?").map_err(|e|e.to_string())?;
                let rows = statement
                    .query_map(
                        params![category, category, query, query, limit, offset],
                        product_row,
                    )
                    .map_err(|e| e.to_string())?;
                Ok((
                    rows.collect::<Result<Vec<_>, _>>()
                        .map_err(|e| e.to_string())?,
                    total,
                ))
            })();
            let _ = reply.send(result);
        }
        DbCommand::Catalog(reply) => {
            let result = (|| {
                let mut result: Vec<CategoryReport> = CATEGORIES
                    .into_iter()
                    .map(|category| CategoryReport {
                        category: category.to_owned(),
                        count: 0,
                        stock: 0,
                        inventory_value_cents: 0,
                    })
                    .collect();
                let mut stmt=connection.prepare("SELECT category,COUNT(*),COALESCE(SUM(stock),0),COALESCE(SUM(stock*price_cents),0) FROM products GROUP BY category").map_err(|e|e.to_string())?;
                let rows = stmt
                    .query_map([], |row| {
                        Ok((
                            row.get::<_, String>(0)?,
                            row.get(1)?,
                            row.get(2)?,
                            row.get(3)?,
                        ))
                    })
                    .map_err(|e| e.to_string())?;
                for row in rows {
                    let (cat, count, stock, value) = row.map_err(|e| e.to_string())?;
                    if let Some(item) = result.iter_mut().find(|x| x.category == cat) {
                        item.count = count;
                        item.stock = stock;
                        item.inventory_value_cents = value;
                    }
                }
                Ok(result)
            })();
            let _ = reply.send(result);
        }
        DbCommand::Events(reply) => {
            let result = (|| {
                let mut totals = empty_events();
                let mut stmt = connection
                    .prepare(
                        "SELECT type,SUM(count),SUM(value_total) FROM event_totals GROUP BY type",
                    )
                    .map_err(|e| e.to_string())?;
                let rows = stmt
                    .query_map([], |row| {
                        Ok((row.get::<_, String>(0)?, row.get(1)?, row.get(2)?))
                    })
                    .map_err(|e| e.to_string())?;
                for row in rows {
                    let (kind, count, value) = row.map_err(|e| e.to_string())?;
                    totals.insert(kind, (count, value));
                }
                Ok(totals)
            })();
            let _ = reply.send(result);
        }
        DbCommand::AddEvents(events, reply) => {
            let result = (|| {
                let tx = connection
                    .transaction_with_behavior(TransactionBehavior::Immediate)
                    .map_err(|e| e.to_string())?;
                {
                    let mut stmt=tx.prepare("INSERT INTO event_totals(user_id,type,count,value_total) VALUES(?,?,1,?) ON CONFLICT(user_id,type) DO UPDATE SET count=count+1,value_total=value_total+excluded.value_total").map_err(|e|e.to_string())?;
                    for event in events {
                        stmt.execute(params![event.user_id, event.r#type, event.value])
                            .map_err(|e| e.to_string())?;
                    }
                }
                tx.commit().map_err(|e| e.to_string())
            })();
            let _ = reply.send(result);
        }
    }
}

fn positive_env(name: &str, fallback: i64, maximum: i64) -> Result<i64, String> {
    env::var(name).map_or(Ok(fallback), |value| {
        value
            .parse::<i64>()
            .ok()
            .filter(|v| (1..=maximum).contains(v))
            .ok_or_else(|| format!("{name} must be an integer between 1 and {maximum}"))
    })
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        if let Ok(mut terminate) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {},
                _ = terminate.recv() => {},
            }
            return;
        }
    }
    let _ = tokio::signal::ctrl_c().await;
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let workers = positive_env("WORKERS", 1, 2)?;
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(usize::try_from(workers)?)
        .enable_all()
        .build()?
        .block_on(run(workers))
}

async fn run(workers: i64) -> Result<(), Box<dyn std::error::Error>> {
    let seed_count = positive_env("SEED_COUNT", 5000, 100000)?;
    let port = positive_env("PORT", 8080, 65535)?;
    let backend_name = env::var("BACKEND").unwrap_or_else(|_| "sqlite".to_owned());
    let db_path = env::var("DB_PATH").unwrap_or_else(|_| "data/benchmark.sqlite".to_owned());
    let (backend, sqlite, join) = match backend_name.as_str() {
        "sqlite" => {
            let (backend, version, join) = sqlite_backend(&db_path, seed_count)?;
            (backend, version, Some(join))
        }
        "memory" => (
            Backend::Memory {
                products: Arc::new((1..=seed_count).map(product_for).collect()),
                events: Arc::new(Mutex::new(BTreeMap::new())),
            },
            "memory".to_owned(),
            None,
        ),
        _ => return Err("BACKEND must be sqlite or memory".into()),
    };
    eprintln!(
        "runtime={} app_version={} sqlite_version={} seed_count={} db_path={} backend={} workers={} router=axum max_open_conns=1 journal_mode=wal synchronous=normal foreign_keys=on busy_timeout=5000 cache_size=-2000 wal_autocheckpoint=1000 temp_store=MEMORY",
        env!("RUSTC_VERSION"),
        env!("CARGO_PKG_VERSION"),
        sqlite,
        seed_count,
        db_path,
        backend_name,
        workers
    );
    let app = Router::new()
        .route("/healthz", get(healthz))
        .route(
            "/products",
            get(list_products).post(|| async { method_not_allowed("GET, HEAD") }),
        )
        .route(
            "/products/{id}",
            get(product_detail).post(|| async { method_not_allowed("GET, HEAD") }),
        )
        .route(
            "/products/",
            get(empty_product_id).post(|| async { method_not_allowed("GET, HEAD") }),
        )
        .route(
            "/reports/catalog",
            get(catalog_report).post(|| async { method_not_allowed("GET, HEAD") }),
        )
        .route(
            "/reports/events",
            get(events_report).post(|| async { method_not_allowed("GET, HEAD") }),
        )
        .route(
            "/cart/quote",
            post(quote).get(|| async { method_not_allowed("POST") }),
        )
        .route(
            "/events/batch",
            post(batch_events).get(|| async { method_not_allowed("POST") }),
        )
        .fallback(|request: Request| async move {
            let _ = request;
            error(StatusCode::NOT_FOUND, "not found")
        })
        .method_not_allowed_fallback(method_not_allowed_for_path)
        .with_state(AppState {
            backend: backend.clone(),
            seed_count,
        });
    let listener =
        tokio::net::TcpListener::bind(SocketAddr::from(([0, 0, 0, 0], u16::try_from(port)?)))
            .await?;
    axum::serve(listener, app.clone())
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    drop(app);
    drop(backend);
    if let Some(worker) = join {
        worker.join().map_err(|_| "sqlite worker panicked")?;
    }
    Ok(())
}
