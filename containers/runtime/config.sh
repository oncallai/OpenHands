# Use the registry from environment variable
DOCKER_REGISTRY=${DOCKER_REGISTRY}
# Use the organization from environment variable
DOCKER_ORG=${DOCKER_ORG}
DOCKER_BASE_DIR="./containers/runtime"
DOCKER_IMAGE=runtime
# These variables will be appended by the runtime_build.py script
# DOCKER_IMAGE_TAG=
# DOCKER_IMAGE_SOURCE_TAG=
