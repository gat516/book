use std::net::SocketAddr;

use textproc::{pb::text_proc_server::TextProcServer, service::TextProcService};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let address: SocketAddr = std::env::var("TEXTPROC_LISTEN_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:50051".to_owned())
        .parse()?;
    let capacity = std::env::var("TEXTPROC_MATCHER_CACHE_CAPACITY")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(128);
    let service = TextProcService::new(capacity);
    let (reporter, health) = tonic_health::server::health_reporter();
    reporter.set_serving::<TextProcServer<TextProcService>>().await;
    println!("textproc listening on {address}");
    tonic::transport::Server::builder()
        .add_service(health)
        .add_service(TextProcServer::new(service))
        .serve(address)
        .await?;
    Ok(())
}
