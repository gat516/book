.PHONY: proto-python proto-check gateway-proto-python gateway-proto-check textproc-check textproc-test textproc-live-test textproc-benchmark

TEXTPROC_TEST_ADDR ?= 127.0.0.1:50051

proto-python:
	services/pipeline/.venv/bin/python -m grpc_tools.protoc -I proto --python_out=services/pipeline/pipeline --grpc_python_out=services/pipeline/pipeline proto/textproc.proto
	sed -i 's/^import textproc_pb2 as /from pipeline import textproc_pb2 as /' services/pipeline/pipeline/textproc_pb2_grpc.py

proto-check: proto-python
	git diff --exit-code -- services/pipeline/pipeline/textproc_pb2.py services/pipeline/pipeline/textproc_pb2_grpc.py

gateway-proto-python:
	services/pipeline/.venv/bin/python -m grpc_tools.protoc -I proto --python_out=packages/novel-llm/src/novel_llm --grpc_python_out=packages/novel-llm/src/novel_llm proto/gateway.proto
	sed -i 's/^import gateway_pb2 as /from novel_llm import gateway_pb2 as /' packages/novel-llm/src/novel_llm/gateway_pb2_grpc.py

gateway-proto-check: gateway-proto-python
	git diff --exit-code -- packages/novel-llm/src/novel_llm/gateway_pb2.py packages/novel-llm/src/novel_llm/gateway_pb2_grpc.py

textproc-check:
	docker compose -f deploy/docker-compose.yml build textproc

textproc-test:
	docker build --target test -f services/textproc/Dockerfile .

textproc-live-test:
	TEXTPROC_TEST_ADDR=$(TEXTPROC_TEST_ADDR) services/pipeline/.venv/bin/pytest -q services/pipeline/tests/test_textproc_live.py

textproc-benchmark:
	PYTHONPATH=services/pipeline services/pipeline/.venv/bin/python services/textproc/benchmark.py --address $(TEXTPROC_TEST_ADDR)
