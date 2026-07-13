#!/bin/sh
set -eu

: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GITHUB_ENV:?GITHUB_ENV is required}"
: "${SOURCE_DATE_EPOCH:?SOURCE_DATE_EPOCH is required}"
: "${NEXT_BUILD_ID:?NEXT_BUILD_ID is required}"
: "${TEXT2SQL_PUBLIC_API_URL:?TEXT2SQL_PUBLIC_API_URL is required}"
: "${CANDIDATE_DIRTY_FLAG:?CANDIDATE_DIRTY_FLAG is required}"
: "${TEXT2SQL_TARGET_POLICY_SHA256:?TEXT2SQL_TARGET_POLICY_SHA256 is required}"

artifacts_directory=${TEXT2SQL_ARTIFACTS_DIR:-artifacts}
build_directory="$artifacts_directory/build"
syft_image=anchore/syft@sha256:8a6ae742083a27abefb827abc5b4b061a0f3748bd832234ed89f2487004a50a0
mkdir -p "$build_directory"

build_once() {
    component=$1
    sequence=$2
    dockerfile=$3
    image_tag=$4
    shift 4
    docker buildx build \
        --no-cache \
        --pull \
        --provenance=false \
        --sbom=false \
        --load \
        --metadata-file "$build_directory/$component-build-$sequence.metadata.json" \
        --file "$dockerfile" \
        --tag "$image_tag" \
        "$@" \
        .
}

metadata_digest() {
    python3 - "$1" "$2" <<'PY'
import json
import re
import sys

value = json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2])
if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
    raise SystemExit(f"Buildx metadata has no canonical {sys.argv[2]}")
print(value)
PY
}

generate_sbom() {
    image_tag=$1
    destination=$2
    docker run --rm \
        --volume /var/run/docker.sock:/var/run/docker.sock \
        "$syft_image" \
        "docker:$image_tag" \
        --output cyclonedx-json > "$destination"
}

verify_component() {
    component=$1
    first_tag=$2
    second_tag=$3
    first_oci_digest=$(metadata_digest "$build_directory/$component-build-1.metadata.json" containerimage.digest)
    second_oci_digest=$(metadata_digest "$build_directory/$component-build-2.metadata.json" containerimage.digest)
    first_config_digest=$(metadata_digest "$build_directory/$component-build-1.metadata.json" containerimage.config.digest)
    second_config_digest=$(metadata_digest "$build_directory/$component-build-2.metadata.json" containerimage.config.digest)
    if [ "$first_oci_digest" != "$second_oci_digest" ]; then
        echo "$component OCI digests differ between clean builds" >&2
        return 1
    fi
    if [ "$first_config_digest" != "$second_config_digest" ]; then
        echo "$component config digests differ between clean builds" >&2
        return 1
    fi

    first_runtime_image=$(docker image inspect "$first_tag" --format '{{.Id}}')
    second_runtime_image=$(docker image inspect "$second_tag" --format '{{.Id}}')
    if [ "$first_runtime_image" != "$second_runtime_image" ]; then
        echo "$component runtime image IDs differ between clean builds" >&2
        return 1
    fi
    if [ "$first_runtime_image" != "$first_config_digest" ]; then
        echo "$component runtime image does not match Buildx config digest" >&2
        return 1
    fi

    first_raw_sbom="$build_directory/$component-sbom-1.cdx.json"
    second_raw_sbom="$build_directory/$component-sbom-2.cdx.json"
    first_normalized_sbom="$build_directory/$component-sbom-1.normalized.cdx.json"
    second_normalized_sbom="$build_directory/$component-sbom-2.normalized.cdx.json"
    generate_sbom "$first_runtime_image" "$first_raw_sbom"
    generate_sbom "$second_runtime_image" "$second_raw_sbom"
    python3 deploy/normalize_sbom.py "$first_raw_sbom" "$first_normalized_sbom" "$first_runtime_image"
    python3 deploy/normalize_sbom.py "$second_raw_sbom" "$second_normalized_sbom" "$second_runtime_image"
    cmp "$first_normalized_sbom" "$second_normalized_sbom"
    sbom_digest=$(sha256sum "$first_normalized_sbom" | cut -d ' ' -f 1)

    python3 - \
        "$component" \
        "$first_oci_digest" \
        "$first_runtime_image" \
        "$sbom_digest" \
        "$build_directory" <<'PY'
import json
from pathlib import Path
import sys

component, oci_digest, runtime_image, sbom_digest, raw_directory = sys.argv[1:]
directory = Path(raw_directory)
payload = {
    "component": component,
    "oci_digest": oci_digest,
    "runtime_image": runtime_image,
    "sbom_sha256": sbom_digest,
    "builds": [
        {
            "metadata": str(directory / f"{component}-build-{sequence}.metadata.json"),
            "oci_digest": oci_digest,
            "config_digest": runtime_image,
        }
        for sequence in (1, 2)
    ],
    "sboms": [
        {
            "path": str(
                directory / f"{component}-sbom-{sequence}.normalized.cdx.json"
            ),
            "sha256": sbom_digest,
        }
        for sequence in (1, 2)
    ],
}
(directory / f"{component}-evidence.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

backend_first_tag="multiagent-text2sql-backend:candidate-1-$GITHUB_SHA"
backend_second_tag="multiagent-text2sql-backend:candidate-2-$GITHUB_SHA"
frontend_first_tag="multiagent-text2sql-frontend:candidate-1-$GITHUB_SHA"
frontend_second_tag="multiagent-text2sql-frontend:candidate-2-$GITHUB_SHA"

build_once backend 1 deploy/Dockerfile.backend "$backend_first_tag" \
    --build-arg "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"
build_once backend 2 deploy/Dockerfile.backend "$backend_second_tag" \
    --build-arg "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"
build_once frontend 1 deploy/Dockerfile.frontend "$frontend_first_tag" \
    --build-arg "NEXT_BUILD_ID=$NEXT_BUILD_ID" \
    --build-arg "NEXT_PUBLIC_AG_UI_URL=$TEXT2SQL_PUBLIC_API_URL" \
    --build-arg "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"
build_once frontend 2 deploy/Dockerfile.frontend "$frontend_second_tag" \
    --build-arg "NEXT_BUILD_ID=$NEXT_BUILD_ID" \
    --build-arg "NEXT_PUBLIC_AG_UI_URL=$TEXT2SQL_PUBLIC_API_URL" \
    --build-arg "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"

verify_component backend "$backend_first_tag" "$backend_second_tag"
verify_component frontend "$frontend_first_tag" "$frontend_second_tag"

python3 - \
    "$build_directory" \
    "$GITHUB_SHA" \
    "$CANDIDATE_DIRTY_FLAG" \
    "$TEXT2SQL_TARGET_POLICY_SHA256" \
    requirements.lock \
    requirements-db-optional.lock \
    frontend/client/package-lock.json <<'PY'
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

directory = Path(sys.argv[1])
commit = sys.argv[2]
dirty_tree = sys.argv[3] == "true"
target_policy_sha256 = sys.argv[4]
lock_paths = [Path(value) for value in sys.argv[5:]]
images = {
    component: json.loads(
        (directory / f"{component}-evidence.json").read_text(encoding="utf-8")
    )
    for component in ("backend", "frontend")
}
candidate_material = json.dumps(
    {component: images[component]["oci_digest"] for component in sorted(images)},
    ensure_ascii=True,
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8")
candidate_digest = hashlib.sha256(candidate_material).hexdigest()
lock_hash = hashlib.sha256()
for path in lock_paths:
    lock_hash.update(path.as_posix().encode("utf-8"))
    lock_hash.update(b"\0")
    lock_hash.update(path.read_bytes())
    lock_hash.update(b"\0")
payload = {
    "schema_version": 1,
    "commit": commit,
    "dirty_tree": dirty_tree,
    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "candidate_digest": candidate_digest,
    "lock_digest": lock_hash.hexdigest(),
    "target_policy_sha256": target_policy_sha256,
    "images": images,
}
(directory / "candidate-metadata.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

python3 - "$build_directory/candidate-metadata.json" "$GITHUB_ENV" <<'PY'
import json
from pathlib import Path
import sys

metadata = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
values = {
    "BACKEND_IMAGE": metadata["images"]["backend"]["runtime_image"],
    "BACKEND_OCI_DIGEST": metadata["images"]["backend"]["oci_digest"],
    "CANDIDATE_DIGEST": metadata["candidate_digest"],
    "CANDIDATE_GENERATED_AT": metadata["generated_at"],
    "FRONTEND_IMAGE": metadata["images"]["frontend"]["runtime_image"],
    "FRONTEND_OCI_DIGEST": metadata["images"]["frontend"]["oci_digest"],
    "LOCK_DIGEST": metadata["lock_digest"],
}
with Path(sys.argv[2]).open("a", encoding="utf-8") as destination:
    for key, value in values.items():
        destination.write(f"{key}={value}\n")
PY
