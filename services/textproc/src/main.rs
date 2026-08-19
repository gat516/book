use std::net::SocketAddr;

use textproc::{pb::text_proc_server::TextProcServer, service::TextProcService};
use tonic_health::pb::{
    HealthCheckRequest, health_check_response::ServingStatus, health_client::HealthClient,
};

async fn health_check() -> Result<(), Box<dyn std::error::Error>> {
    let endpoint = std::env::var("TEXTPROC_HEALTH_ADDR")
        .unwrap_or_else(|_| "http://127.0.0.1:50051".to_owned());
    let channel = tonic::transport::Endpoint::from_shared(endpoint)?
        .connect()
        .await?;
    let mut client = HealthClient::new(channel);
    let response = client
        .check(HealthCheckRequest {
            service: String::new(),
        })
        .await?
        .into_inner();
    if response.status != ServingStatus::Serving as i32 {
        return Err(format!("textproc health status is {}", response.status).into());
    }
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("install Ctrl-C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        () = ctrl_c => {},
        () = terminate => {},
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::args().nth(1).as_deref() == Some("--health-check") {
        return health_check().await;
    }
    let address: SocketAddr = std::env::var("TEXTPROC_LISTEN_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:50051".to_owned())
        .parse()?;
    let capacity = std::env::var("TEXTPROC_MATCHER_CACHE_CAPACITY")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(128);
    let service = TextProcService::new(capacity);
    let (reporter, health) = tonic_health::server::health_reporter();
    reporter
        .set_serving::<TextProcServer<TextProcService>>()
        .await;
    println!("textproc listening on {address}");
    tonic::transport::Server::builder()
        .add_service(health)
        .add_service(TextProcServer::new(service))
        .serve_with_shutdown(address, shutdown_signal())
        .await?;
    Ok(())
}
