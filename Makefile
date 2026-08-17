.PHONY: proto-python proto-check textproc-check textproc-test

proto-python:
	services/pipeline/.venv/bin/python -m grpc_tools.protoc -I proto --python_out=services/pipeline/pipeline --grpc_python_out=services/pipeline/pipeline proto/textproc.proto
	sed -i 's/^import textproc_pb2 as /from pipeline import textproc_pb2 as /' services/pipeline/pipeline/textproc_pb2_grpc.py

proto-check: proto-python
	git diff --exit-code -- services/pipeline/pipeline/textproc_pb2.py services/pipeline/pipeline/textproc_pb2_grpc.py

textproc-check:
	docker compose -f deploy/docker-compose.yml build textproc

textproc-test:
	docker build --target test -f services/textproc/Dockerfile .
