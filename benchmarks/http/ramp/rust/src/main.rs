use std::{net::IpAddr, sync::Arc};

use actix_web::{App, HttpRequest, HttpResponse, HttpServer, web};
use axum::{
    Router,
    body::Bytes,
    extract::{Path, State},
    http::{HeaderMap, StatusCode, header},
    response::Response,
    routing::{get, post},
};
use performance_http_ramp_rust::{
    ResultBody, Store, header as timing_header, list_query, setting, setting_int, shared_store,
    stock_body, valid_id,
};
use rocket::{
    State as RocketState,
    data::{Data, ToByteUnit},
    http::{ContentType, Status},
    response::Responder,
    routes,
};

fn response(result: ResultBody) -> Response {
    let status = StatusCode::from_u16(result.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    let mut builder = Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "application/json");
    if let Some(value) = timing_header(&result) {
        builder = builder.header("server-timing", value)
    };
    builder.body(axum::body::Body::from(result.body)).unwrap()
}

async fn axum_health() -> Response {
    response(ResultBody {
        status: 200,
        body: b"{\"status\":\"ok\"}".to_vec(),
        service_ms: 0.0,
        db_ms: 0.0,
        timed: false,
    })
}
async fn axum_info(State(store): State<Arc<Store>>) -> Response {
    response(store.info("axum").await)
}
async fn axum_integrity(State(store): State<Arc<Store>>) -> Response {
    response(store.integrity().await)
}
async fn axum_detail(State(store): State<Arc<Store>>, Path(raw): Path<String>) -> Response {
    match valid_id(&raw) {
        Ok(id) => response(store.detail(id).await),
        Err(e) => response(e),
    }
}
async fn axum_list(State(store): State<Arc<Store>>, request: axum::extract::Request) -> Response {
    match list_query(request.uri().query(), store.seed) {
        Ok((offset, limit)) => response(store.list(offset, limit).await),
        Err(e) => response(e),
    }
}
async fn axum_stock(
    State(store): State<Arc<Store>>,
    Path(raw): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let id = match valid_id(&raw) {
        Ok(v) => v,
        Err(e) => return response(e),
    };
    match stock_body(
        headers
            .get(header::CONTENT_TYPE)
            .and_then(|h| h.to_str().ok()),
        &body,
    ) {
        Ok(delta) => response(store.update(id, delta).await),
        Err(e) => response(e),
    }
}

async fn run_axum(store: Arc<Store>, port: u16) {
    let app = Router::new()
        .route("/healthz", get(axum_health))
        .route("/benchmark/info", get(axum_info))
        .route("/benchmark/integrity", get(axum_integrity))
        .route("/products", get(axum_list))
        .route("/products/{id}", get(axum_detail))
        .route("/products/{id}/stock", post(axum_stock))
        .with_state(store);
    let listener = tokio::net::TcpListener::bind((IpAddr::from([0, 0, 0, 0]), port))
        .await
        .unwrap();
    axum::serve(listener, app).await.unwrap();
}

fn actix_response(result: ResultBody) -> HttpResponse {
    let status = actix_web::http::StatusCode::from_u16(result.status).unwrap();
    let mut builder = HttpResponse::build(status);
    builder.content_type("application/json");
    if let Some(value) = timing_header(&result) {
        builder.insert_header(("server-timing", value));
    }
    builder.body(result.body)
}
async fn actix_health() -> HttpResponse {
    actix_response(ResultBody {
        status: 200,
        body: b"{\"status\":\"ok\"}".to_vec(),
        service_ms: 0.0,
        db_ms: 0.0,
        timed: false,
    })
}
async fn actix_info(store: web::Data<Arc<Store>>) -> HttpResponse {
    actix_response(store.info("actix").await)
}
async fn actix_integrity(store: web::Data<Arc<Store>>) -> HttpResponse {
    actix_response(store.integrity().await)
}
async fn actix_detail(store: web::Data<Arc<Store>>, path: web::Path<String>) -> HttpResponse {
    match valid_id(&path) {
        Ok(id) => actix_response(store.detail(id).await),
        Err(e) => actix_response(e),
    }
}
async fn actix_list(store: web::Data<Arc<Store>>, request: HttpRequest) -> HttpResponse {
    match list_query(request.uri().query(), store.seed) {
        Ok((o, l)) => actix_response(store.list(o, l).await),
        Err(e) => actix_response(e),
    }
}
async fn actix_stock(
    store: web::Data<Arc<Store>>,
    path: web::Path<String>,
    request: HttpRequest,
    body: web::Bytes,
) -> HttpResponse {
    let id = match valid_id(&path) {
        Ok(v) => v,
        Err(e) => return actix_response(e),
    };
    match stock_body(
        request
            .headers()
            .get("content-type")
            .and_then(|h| h.to_str().ok()),
        &body,
    ) {
        Ok(d) => actix_response(store.update(id, d).await),
        Err(e) => actix_response(e),
    }
}
async fn run_actix(store: Arc<Store>, port: u16) -> std::io::Result<()> {
    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(store.clone()))
            .route("/healthz", web::get().to(actix_health))
            .route("/benchmark/info", web::get().to(actix_info))
            .route("/benchmark/integrity", web::get().to(actix_integrity))
            .route("/products", web::get().to(actix_list))
            .route("/products/{id}", web::get().to(actix_detail))
            .route("/products/{id}/stock", web::post().to(actix_stock))
    })
    .workers(1)
    .bind(("0.0.0.0", port))?
    .run()
    .await
}

struct RocketResult(ResultBody);
struct RawQuery(Option<String>);

#[rocket::async_trait]
impl<'r> rocket::request::FromRequest<'r> for RawQuery {
    type Error = ();

    async fn from_request(
        request: &'r rocket::Request<'_>,
    ) -> rocket::request::Outcome<Self, Self::Error> {
        rocket::request::Outcome::Success(Self(
            request.uri().query().map(|query| query.as_str().to_owned()),
        ))
    }
}

impl<'r> Responder<'r, 'static> for RocketResult {
    fn respond_to(self, _: &'r rocket::Request<'_>) -> rocket::response::Result<'static> {
        let mut response = rocket::Response::build();
        response.status(Status::from_code(self.0.status).unwrap_or(Status::InternalServerError));
        response.header(ContentType::JSON);
        if let Some(value) = timing_header(&self.0) {
            response.raw_header("Server-Timing", value);
        };
        response.sized_body(self.0.body.len(), std::io::Cursor::new(self.0.body));
        response.ok()
    }
}
#[rocket::get("/healthz")]
fn rocket_health() -> RocketResult {
    RocketResult(ResultBody {
        status: 200,
        body: b"{\"status\":\"ok\"}".to_vec(),
        service_ms: 0.0,
        db_ms: 0.0,
        timed: false,
    })
}
#[rocket::get("/benchmark/info")]
async fn rocket_info(store: &RocketState<Arc<Store>>) -> RocketResult {
    RocketResult(store.info("rocket").await)
}
#[rocket::get("/benchmark/integrity")]
async fn rocket_integrity(store: &RocketState<Arc<Store>>) -> RocketResult {
    RocketResult(store.integrity().await)
}
#[rocket::get("/products/<raw>")]
async fn rocket_detail(store: &RocketState<Arc<Store>>, raw: String) -> RocketResult {
    RocketResult(match valid_id(&raw) {
        Ok(id) => store.detail(id).await,
        Err(e) => e,
    })
}
#[rocket::get("/products")]
async fn rocket_list(store: &RocketState<Arc<Store>>, query: RawQuery) -> RocketResult {
    RocketResult(match list_query(query.0.as_deref(), store.seed) {
        Ok((o, l)) => store.list(o, l).await,
        Err(e) => e,
    })
}
#[rocket::post("/products/<raw>/stock", data = "<data>")]
async fn rocket_stock(
    store: &RocketState<Arc<Store>>,
    raw: String,
    content_type: Option<&ContentType>,
    data: Data<'_>,
) -> RocketResult {
    let id = match valid_id(&raw) {
        Ok(v) => v,
        Err(e) => return RocketResult(e),
    };
    let body = data.open(65_537_u64.bytes()).into_bytes().await;
    let content = content_type.map(ToString::to_string);
    match body {
        Ok(body) => RocketResult(match stock_body(content.as_deref(), &body.value) {
            Ok(d) => store.update(id, d).await,
            Err(e) => e,
        }),
        Err(_) => RocketResult(ResultBody {
            status: 400,
            body: b"{\"error\":\"invalid JSON body\"}".to_vec(),
            service_ms: 0.0,
            db_ms: 0.0,
            timed: false,
        }),
    }
}
async fn run_rocket(store: Arc<Store>, port: u16) {
    let config = rocket::Config {
        address: std::net::IpAddr::from([0, 0, 0, 0]),
        port,
        workers: 1,
        max_blocking: 1,
        log_level: rocket::config::LogLevel::Off,
        ..rocket::Config::default()
    };
    rocket::custom(config)
        .manage(store)
        .mount(
            "/",
            routes![
                rocket_health,
                rocket_info,
                rocket_integrity,
                rocket_detail,
                rocket_list,
                rocket_stock
            ],
        )
        .launch()
        .await
        .unwrap();
}

#[tokio::main(flavor = "multi_thread", worker_threads = 1)]
async fn main() {
    let framework = setting("FRAMEWORK", "axum");
    assert!(
        matches!(framework.as_str(), "axum" | "actix" | "rocket"),
        "FRAMEWORK must be axum, actix, or rocket"
    );
    let seed = setting_int("SEED_COUNT", 5000, 100, 100000);
    let port = setting_int("PORT", 8080, 1, 65535) as u16;
    let path = setting("SQLITE_PATH", "/data/benchmark.sqlite");
    let store = shared_store(
        Store::open(&path, seed).unwrap_or_else(|e| panic!("database initialization failed: {e}")),
    );
    eprintln!(
        "experiment=sqlite-ramp-v2 runtime=rust framework={framework} executor_workers=1 database_worker=dedicated-thread"
    );
    match framework.as_str() {
        "axum" => run_axum(store, port).await,
        "actix" => run_actix(store, port).await.unwrap(),
        "rocket" => run_rocket(store, port).await,
        _ => unreachable!(),
    }
}
