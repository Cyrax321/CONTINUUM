# TLS Reverse Proxy Guide for CONTINUUM Sidecar

The CONTINUUM sidecar (`continuum serve --transport http`) exposes an HTTP wire
protocol designed for local or container-adjacent durability control. In production
deployments spanning network boundaries, the sidecar should run behind a TLS-terminating
reverse proxy rather than exposing raw HTTP directly.

This guide provides tested configurations for Nginx and Caddy to terminate TLS,
forward client authorization headers, and enforce network boundary security.

---

## Architecture Overview

```
                      HTTPS / TLS
External Client / Agent ----------> Reverse Proxy (Nginx / Caddy)
                                     - TLS Termination (Cert / Key)
                                     - Authorization Header Forwarding
                                     - 1 MB Body Limit Enforcement
                                           |
                                           | HTTP (loopback)
                                           v
                                    CONTINUUM Sidecar
                                    (127.0.0.1:8765)
```

The sidecar binds to loopback (`127.0.0.1`) by default. The reverse proxy accepts
inbound HTTPS traffic from clients, terminates TLS, and forwards requests to the
sidecar over the local interface.

---

## Authentication and Credentials

The sidecar supports two distinct credential tiers:

1. **Client Tokens (`CONTINUUM_SERVE_CLIENT_TOKENS` or `CONTINUUM_SERVE_TOKEN`)**:
   Used by agent processes for normal operations (`record_progress`, `checkpoint`,
   `resume`, `intercept_action`, `complete_action`). Sent via standard HTTP header:
   `Authorization: Bearer <client-token>`.

2. **Confirm Token (`CONTINUUM_SERVE_CONFIRM_TOKEN`)**:
   Used exclusively for human review confirmations (`confirm`). An agent holding
   a normal client token cannot call `confirm`. Sent via `Authorization: Bearer <confirm-token>`.

All token comparisons use constant-time matching (`hmac.compare_digest`) on the server.

---

## Nginx Configuration

Save the following configuration as `/etc/nginx/conf.d/continuum.conf`:

```nginx
# Upstream definition for CONTINUUM sidecar
upstream continuum_sidecar {
    server 127.0.0.1:8765;
    keepalive 32;
}

server {
    listen 443 ssl http2;
    server_name continuum.internal.example.com;

    # TLS Certificates and Protocols
    ssl_certificate /etc/ssl/certs/continuum.crt;
    ssl_certificate_key /etc/ssl/private/continuum.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    # Body Limit (matched to MAX_SIDECAR_BODY_BYTES: 1 MB)
    client_max_body_size 1M;

    location / {
        proxy_pass http://continuum_sidecar;
        proxy_http_version 1.1;

        # Header forwarding (preserve Authorization for bearer tokens)
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Keepalive and timeouts
        proxy_set_header Connection "";
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
    }
}
```

### Optional: Mutual TLS (mTLS) with Nginx

To restrict access so that only clients with a verified certificate can reach the proxy:

```nginx
    # Add inside the server block:
    ssl_client_certificate /etc/ssl/certs/ca-client.crt;
    ssl_verify_client on;
```

---

## Caddy Configuration

Caddy handles automatic TLS certificate provisioning and renewal. Add this block
to your `Caddyfile`:

```caddy
continuum.internal.example.com {
    # Match the 1 MB body limit of the sidecar
    request_body {
        max_size 1MB
    }

    # Reverse proxy to the local CONTINUUM sidecar
    reverse_proxy 127.0.0.1:8765 {
        # Caddy automatically forwards Host, X-Forwarded-For, and Authorization headers
        header_up Host {host}
        header_up X-Forwarded-Proto {scheme}

        transport http {
            keepalive 30s
            keepalive_idle_conns 32
        }
    }
}
```

---

## Starting the Service

1. Configure environment variables for authentication and bind address:

```bash
export CONTINUUM_DB="/var/lib/continuum/continuum.db"
export CONTINUUM_SERVE_BIND="127.0.0.1"
export CONTINUUM_SERVE_CLIENT_TOKENS="agent-token-alpha,agent-token-beta"
export CONTINUUM_SERVE_CONFIRM_TOKEN="supervisor-human-token-secure"
```

2. Launch the sidecar service:

```bash
continuum serve --transport http --port 8765 --bind 127.0.0.1
```

3. Reload the reverse proxy:

```bash
# For Nginx:
sudo nginx -t && sudo systemctl reload nginx

# For Caddy:
caddy reload
```

4. Verify connectivity:

```bash
# Agent call with client token (allowed):
curl -s -X POST https://continuum.internal.example.com/record_progress \
  -H "Authorization: Bearer agent-token-alpha" \
  -H "Content-Type: application/json" \
  -d '{"run_id": "test-run", "completed": 1, "goal": "migrate database"}'

# Agent call to confirm with normal client token (refused with 403 Forbidden):
curl -s -X POST https://continuum.internal.example.com/confirm \
  -H "Authorization: Bearer agent-token-alpha" \
  -H "Content-Type: application/json" \
  -d '{"run_id": "test-run"}'

# Supervisor call to confirm with dedicated confirm token (allowed with 200 OK):
curl -s -X POST https://continuum.internal.example.com/confirm \
  -H "Authorization: Bearer supervisor-human-token-secure" \
  -H "Content-Type: application/json" \
  -d '{"run_id": "test-run"}'
```
