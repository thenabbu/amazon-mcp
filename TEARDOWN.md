# Teardown — temporary public endpoint amazon-mcp.200871.xyz

Deployed 2026-10-23. Remove all three pieces (in any order):

## 1. DNS record
```bash
# zone 200871.xyz = e7e0257dc67334eaea9e52603bd8adbf, record id 4d56d10087a9d51eccfdad7995305db4
CF_TOKEN=$(grep '^CF_FULL_TOKEN=' /docker/.env | sed -n '21p' | cut -d= -f2- | cut -d'#' -f1 | tr -d ' \r\n')  # on lab host; line 21
curl -s -X DELETE "https://api.cloudflare.com/client/v4/zones/e7e0257dc67334eaea9e52603bd8adbf/dns_records/4d56d10087a9d51eccfdad7995305db4" \
  -H "Authorization: Bearer $CF_TOKEN"
```

## 2. Tunnel ingress rule (tunnel "homelab", id c2308be7-6527-4da3-a96c-49c4b4c918c0)
GET the configurations, drop the object `{"hostname":"amazon-mcp.200871.xyz",...}`,
PUT the rest back (NEVER PUT a list you didn't just GET — see cloudflare-tunnel-access-api skill).
No cloudflared restart needed: token-managed tunnels pick up config pushes automatically.

## 3. Service container (host-networked, port 8787, restart unless-stopped)
```bash
docker stop amazon-mcp && docker rm amazon-mcp
# host source files (safe to keep): /home/nabbu/.hermes/projects/amazon-mcp  (= hermes /opt/data/projects/amazon-mcp)
```

No Cloudflare Access app was created for this hostname. flights.200871.xyz was never touched.
