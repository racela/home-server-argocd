#!/usr/bin/env bash
# Load ECR's Docker Official Images into kind before Helm waits for pods. On
# shared runners ECR can return 429; Google's Docker Hub mirror provides the
# same official repository/tag. Keep the chart's original image reference.
set -euo pipefail

cluster="$1"
shift
archive=$(mktemp)
trap 'rm -f "$archive"' EXIT

for image in "$@"; do
  case "$image" in
    ecr-public.aws.com/docker/library/*)
      suffix="${image#ecr-public.aws.com/docker/library/}"
      ;;
    public.ecr.aws/docker/library/*)
      suffix="${image#public.ecr.aws/docker/library/}"
      ;;
    *) continue ;;
  esac

  if ! docker pull "$image"; then
    mirror="mirror.gcr.io/library/$suffix"
    docker pull "$mirror"
    docker tag "$mirror" "$image"
  fi
  # Export only the ubuntu-latest runner's platform. With Docker's containerd
  # store, kind load docker-image can reference missing multi-platform blobs.
  # https://kind.sigs.k8s.io/docs/user/known-issues/#unable-to-kind-load-docker-images
  docker image save --platform linux/amd64 --output "$archive" "$image"
  kind load image-archive --name "$cluster" "$archive"
done
