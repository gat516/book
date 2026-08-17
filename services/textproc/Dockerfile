FROM rust:1.88-bookworm AS build
WORKDIR /src
COPY proto /src/proto
COPY services/textproc /src/services/textproc
WORKDIR /src/services/textproc
RUN cargo build --release

FROM build AS test
RUN cargo test

FROM debian:bookworm-slim
COPY --from=build /src/services/textproc/target/release/textproc /usr/local/bin/textproc
EXPOSE 50051
ENV TEXTPROC_LISTEN_ADDR=0.0.0.0:50051
ENTRYPOINT ["/usr/local/bin/textproc"]
