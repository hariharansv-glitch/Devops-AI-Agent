// ----------------------------------------------------------------------
// AI DevOps Assistant — CI/CD pipeline
//
// This is a Multibranch Pipeline. Jenkins's "Declarative: Checkout SCM"
// stage (added automatically by Jenkins) already clones the repo for us.
// Do NOT add a manual `cleanWs()` + `git ...` checkout — that would wipe
// the workspace and then try to clone from a different URL.
//
// What this pipeline does:
//   1. Ensures the Docker Compose v2 plugin is installed on the agent.
//   2. Materialises secrets from Jenkins credentials into a .env file and
//      the target-VM SSH key into ./keys/ (both consumed by compose).
//   3. Builds the FastAPI image (multi-stage) and brings the service up.
//   4. Waits for the container healthcheck to report healthy.
//   5. Runs in-container smoke tests against /healthz and /api/info.
//   6. Prints the live URL on success, or dumps logs on failure.
//
// The app is a Python 3.12 / FastAPI service (ADK-based DevOps agent). It
// listens on container port 5500 and exposes /healthz, /api/info, and a
// web UI at /.
//
// ---- Jenkins credentials this pipeline expects ----
//   * groq-api-key      (Secret text) -> GROQ_API_KEY
//   * opc-ssh-key-file  (Secret file) -> the target-VM SSH private key.
//     Uploaded as a file (NOT typed into a text area) to side-step the
//     "SSH Username with private key" UI trap where updates silently
//     discard the pasted body. The username is hard-wired to
//     AGENT_VM_USER below.
// Create them under: Manage Jenkins > Credentials. If you use Gemini
// instead of Groq, swap the credential for a `google-api-key` secret and
// set MODEL_NAME accordingly below.
// ----------------------------------------------------------------------
pipeline {
    agent any

    options {
        timestamps()
        timeout(time: 20, unit: 'MINUTES')
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '20', artifactNumToKeepStr: '5'))
    }

    environment {
        // ---- Deploy target (where users hit the assistant in the browser) ----
        VM_HOST = '140.245.254.149'

        // ---- Host port mapping (interpolated by docker-compose.yml) ----
        // The container always listens on 5500 internally; this is the host
        // port that's published. Pick a port that is free on the VM.
        HOST_PORT = '5500'

        // ---- Which LLM the agent uses (see .env.example for options) ----
        MODEL_NAME = 'groq/llama-3.3-70b-versatile'

        // ---- Target Linux VM the agent inspects over SSH ----
        // Because docker-compose runs the container with `network_mode: host`,
        // the container shares the VM's network stack — so the VM is reachable
        // as 127.0.0.1 from inside the container. This side-steps the OCI /
        // cloud-NAT hairpin problem where hitting the VM's own PUBLIC IP from
        // inside a Docker bridge network fails. If you point this pipeline at
        // a *different* VM later, drop `network_mode: host` from compose and
        // set AGENT_VM_HOST to that VM's reachable address.
        AGENT_VM_HOST = '127.0.0.1'
        AGENT_VM_USER = 'opc'
        AGENT_VM_PORT = '22'

        // TRUE = block ALL destructive tools even with confirmation.
        // FALSE = allow read + write (destructive ops still need confirmation).
        READ_ONLY_MODE = 'FALSE'

        TZ = 'UTC'

        // Stable Compose project name so containers always get the same
        // names (matches `name: devops-ai-agent` in docker-compose.yml).
        COMPOSE_PROJECT_NAME = 'devops-ai-agent'

        // Container name we poll for health (matches container_name in compose).
        WEB_CONTAINER = 'devops-ai-agent'
    }

    stages {

        stage('Verify Docker') {
            steps {
                sh '''
                set -e

                docker --version

                if ! docker compose version >/dev/null 2>&1; then
                    echo "Installing Docker Compose plugin..."

                    ARCH=$(uname -m)
                    mkdir -p $HOME/.docker/cli-plugins

                    curl -fsSL \
                      https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-${ARCH} \
                      -o $HOME/.docker/cli-plugins/docker-compose

                    chmod +x $HOME/.docker/cli-plugins/docker-compose
                fi

                docker compose version
                '''
            }
        }

        stage('Generate .env & SSH key') {
            steps {
                // Bind the target-VM SSH key and the LLM API key from Jenkins
                // credentials so nothing sensitive is ever committed or echoed.
                withCredentials([
                    file(credentialsId: 'opc-ssh-key-file', variable: 'VM_KEY_FILE'),
                    string(credentialsId: 'groq-api-key', variable: 'GROQ_API_KEY')
                ]) {
                    sh '''
                    set -e

                    # Diagnostics: show what Jenkins actually gave us BEFORE
                    # we try to use it. The full path is masked by Jenkins
                    # (Jenkins auto-masks any variable value that contains a
                    # credential), so we log the SIZE and the HEADER LINE only
                    # (headers like `-----BEGIN RSA PRIVATE KEY-----` are not
                    # secret and are the fastest way to spot a malformed key).
                    SRC_SIZE=$(stat -c '%s' "$VM_KEY_FILE" 2>/dev/null || echo 0)
                    SRC_HEADER=$(head -c 40 "$VM_KEY_FILE" 2>/dev/null | tr -d '\r' | head -1)
                    echo "Source key size: ${SRC_SIZE} bytes"
                    echo "Source key header: ${SRC_HEADER}"

                    if [ "${SRC_SIZE}" = "0" ]; then
                        cat >&2 <<'MSG'
ERROR: Jenkins wrote a 0-byte file for the 'opc-ssh-key-file' credential.

Fix:
  1. Manage Jenkins > Credentials > System > Global credentials.
  2. Delete any pre-existing 'opc-ssh-key-file' entry.
  3. Add credential > Kind: 'Secret file'.
        ID:   opc-ssh-key-file
        File: click 'Choose File' and select the target-VM private key
              on your local machine (e.g. ssh-key-2026-06-11.key).
  4. Save, then re-run this pipeline.
MSG
                        exit 1
                    fi

                    # Stage the private key in the workspace with world-read
                    # (644) so we can stream it to the helper container. 644 is
                    # safe because the workspace is ephemeral and paramiko
                    # (unlike OpenSSH) does not enforce mode.
                    mkdir -p keys
                    install -m 644 "$VM_KEY_FILE" keys/target_vm_key

                    # Belt & suspenders: reject anything that doesn't look like
                    # a PEM/OpenSSH private key. Catches the rare case where
                    # someone pasted a *public* key or a random blob.
                    if ! head -1 keys/target_vm_key | grep -qE '^-----BEGIN( OPENSSH| RSA| EC| DSA| ENCRYPTED)? PRIVATE KEY-----$'; then
                        echo "ERROR: keys/target_vm_key does not start with a PEM/OpenSSH PRIVATE KEY header." >&2
                        echo "First line was: $(head -1 keys/target_vm_key)" >&2
                        exit 1
                    fi

                    # Populate the NAMED VOLUME (devops_keys) by piping the key
                    # over Docker's socket via stdin. Do NOT use `-v $PWD/keys:/src`
                    # here: when Jenkins itself runs inside a container (as here),
                    # the workspace lives on a Docker volume and is NOT a host
                    # path, so bind mounts from it silently mount an empty
                    # directory. The stdin approach side-steps that entirely.
                    # `docker volume create` is idempotent, so this is safe on
                    # every rebuild. mode 644 on the copy keeps it readable by
                    # the container's non-root `app` user (uid 1000).
                    docker volume create devops_keys >/dev/null
                    docker run --rm -i \
                        -v devops_keys:/dest \
                        busybox:latest sh -c '
                            cat > /dest/target_vm_key &&
                            chmod 644 /dest/target_vm_key &&
                            echo "Key written to volume ($(wc -c < /dest/target_vm_key) bytes)"
                        ' < keys/target_vm_key

                    # The Secret-file credential contains only the key body;
                    # the username lives in the pipeline environment block.
                    SSH_USER="${AGENT_VM_USER}"

                    cat > .env <<EOF
# ---- LLM provider ----
MODEL_NAME=${MODEL_NAME}
GROQ_API_KEY=${GROQ_API_KEY}

# ---- Target Linux VM (inspected over SSH) ----
VM_HOST=${AGENT_VM_HOST}
VM_PORT=${AGENT_VM_PORT}
VM_USER=${SSH_USER}
VM_PRIVATE_KEY=/keys/target_vm_key
SSH_AUTO_ADD_HOST_KEYS=TRUE

# ---- Application / API ----
APP_NAME=ai-devops-assistant
APP_ENV=production
API_HOST=0.0.0.0
API_PORT=5500
CORS_ORIGINS=*

# ---- Host port + safety ----
HOST_PORT=${HOST_PORT}
READ_ONLY_MODE=${READ_ONLY_MODE}

# ---- Misc ----
TZ=${TZ}
LOG_LEVEL=INFO
LOG_DIR=logs
LOG_JSON=FALSE
EOF

                    echo ".env written (secrets masked):"
                    sed 's/\\(KEY\\|PASSWORD\\|TOKEN\\)=.*/\\1=***/I' .env
                    '''
                }
            }
        }

        stage('Build & Deploy') {
            steps {
                sh '''
                set -e

                docker compose down --remove-orphans || true

                # Use the build cache for speed. Switch to --no-cache only when
                # you really need a clean rebuild (e.g. base-image security patch
                # or a stale pip layer).
                docker compose build

                docker compose up -d

                docker image prune -f
                '''
            }
        }

        stage('Wait for Web') {
            steps {
                sh '''
                echo "Waiting for ${WEB_CONTAINER} to report healthy..."

                for i in $(seq 1 60); do
                    STATUS=$(docker inspect -f '{{.State.Health.Status}}' ${WEB_CONTAINER} 2>/dev/null || echo "starting")

                    if [ "$STATUS" = "healthy" ]; then
                        echo "Web service is healthy."
                        exit 0
                    fi

                    if [ "$STATUS" = "unhealthy" ]; then
                        echo "Web service reported unhealthy."
                        docker compose logs devops-assistant
                        exit 1
                    fi

                    sleep 5
                done

                echo "Web service failed to become healthy within timeout."
                docker compose logs
                exit 1
                '''
            }
        }

        stage('Smoke Test') {
            steps {
                sh '''
                set -e

                # We run the smoke test INSIDE the container via `docker exec`
                # rather than from the Jenkins agent. Reason: when Jenkins
                # itself runs in a container, its 127.0.0.1 is its own
                # loopback — not the host where the app publishes port 5500.
                # Running inside the container uses the app's own listener.

                echo "Checking /healthz from inside ${WEB_CONTAINER}..."

                STATUS=$(docker exec ${WEB_CONTAINER} \
                    python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:5500/healthz', timeout=5).status)")

                if [ "$STATUS" != "200" ]; then
                    echo "Unexpected HTTP status from /healthz : ${STATUS:-no-response}"
                    docker compose logs --tail=100 devops-assistant || true
                    exit 1
                fi
                echo "/healthz returned $STATUS — OK."

                # /api/info returns service metadata as JSON — a good check that
                # the app booted its routes (not just the health probe).
                echo "Checking /api/info..."
                STATUS=$(docker exec ${WEB_CONTAINER} \
                    python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:5500/api/info', timeout=5).status)")

                if [ "$STATUS" != "200" ]; then
                    echo "/api/info returned ${STATUS:-no-response} — expected 200."
                    docker compose logs --tail=100 devops-assistant || true
                    exit 1
                fi
                echo "/api/info returned 200 — OK."
                '''
            }
        }

        stage('Verify Containers') {
            steps {
                sh 'docker compose ps'
            }
        }
    }

    post {

        success {
            echo "Deployment Successful"
            echo "Web UI   : http://${VM_HOST}:${HOST_PORT}/"
            echo "Health   : http://${VM_HOST}:${HOST_PORT}/healthz"
            echo "API docs : http://${VM_HOST}:${HOST_PORT}/docs"
        }

        failure {
            echo "Deployment Failed"
            sh '''
            docker compose logs --tail=200 || true
            docker compose ps              || true
            '''
        }

        always {
            // Remove the materialised secrets from the workspace so they never
            // linger on the agent between builds.
            sh '''
            rm -f .env keys/target_vm_key || true
            docker ps -a
            '''
        }
    }
}
