# Runbook: stale feed

**Symptoms:** the live view is grey with "DATA STALE" or "No feed"; the fleet board shows a rig
as stale; Prometheus alerts `FeedSilent`, `FeedLatencyHigh` (p95 > 10 s) or
`AlertEvaluationSlow` (p99 > 5 s).

**What it means for the rig:** while the feed is stale the real-time service pauses its
detectors (no new alerts from stale data) and the UI says so. Hazard roadmap and lookahead still
work. Tell the wellsite engineer that real-time alerting is off until the feed is back; the
driller's own instruments are unaffected (the stack never writes to rig systems).

## 1. Where is it stuck?

```bash
docker compose ps realtime witsml-sim
```

```bash
docker compose logs --since 15m realtime
```

| Log line | Meaning | Go to |
|---|---|---|
| `connector witsml failed: ... retrying in N s` | The WITSML store does not answer | 2 |
| `CERTIFICATE_VERIFY_FAILED` | The store's certificate is not trusted | 3 |
| no errors, samples arriving, latency high | Clock skew or a slow database | 4 |
| `context reload failed` | Database unreachable from the service | [Database restore](db-restore.md), step 1 |

In Grafana (Service levels), compare *Feed latency* with *Alert evaluation*: latency high with
evaluation normal means the delay is before us (rig, link, clock); both high means we are slow.

## 2. The WITSML store does not answer

From the real-time container (same network path; put the host and port of `RT_WITSML_URL`):

```bash
docker compose exec realtime python -c "import urllib.request as u; print(u.urlopen('https://witsml-sim:8090/', timeout=5).status)"
```

Any HTTP status (even 404 or 405) means the store answers; a timeout or "connection refused"
means it does not.

- Timeout on a rig box: check the rig LAN and that `infra/firewall/no-egress.sh` was run with the
  store's address and port. A store that moved needs a new `RT_WITSML_URL` and a firewall rule.
- Refused: the rig's WITSML server is down. Call the rig's data engineer (mud logger / MWD).
- The service reconnects by itself with back-off up to 30 s; no restart needed once the store is
  back. The gap is recorded in `rt_gap` and shown on the rig-state timeline.

## 3. Certificate not trusted

The rig's store presents a certificate the containers do not trust. Add the store's CA to the
stack's trust list (`/ca/trust/` in the `ca` volume) and rebuild the bundles:

```bash
docker compose run --rm --no-deps -T --entrypoint sh init -c "mkdir -p /ca/trust && cat > /ca/trust/rig-witsml.pem" < rig-witsml-ca.pem
```

```bash
docker compose run --rm init
```

```bash
docker compose restart realtime
```

Do not disable verification.

## 4. Samples arrive late

- **Clock skew:** feed latency is measured against the sample's own timestamp. Compare the rig
  server's clock with this host (`date -u` on both). Fix NTP on whichever is wrong; the
  numbers recover at once.
- **Database slow:** `Alert evaluation` p99 up as well. Check Postgres CPU and disk
  (`docker stats postgres`) and long queries:

```bash
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT pid, now() - query_start AS age, left(query, 80) FROM pg_stat_activity WHERE state = 'active' ORDER BY age DESC LIMIT 10"
```

- **VSAT, rig box:** feed latency on the rig box itself is unaffected by the uplink (the store is
  on the rig LAN). At headquarters, a rig shown stale on the fleet board while its own box is
  live means only the uplink is down: see [Rig Edge Pack](../edge.md#when-the-link-is-down).

## 5. Close

The live view turns live within one sample of data returning. Check that `FeedSilent` has
cleared and note the gap (from `rt_gap`) in the shift handover.
