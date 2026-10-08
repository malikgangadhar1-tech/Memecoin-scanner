"""Anti-rug LABELS for v3 pool alerts (Rohit Oct 8 09:16 IST: "dev holding 1% max should be anti rug").
LABEL ONLY until graded at v3 n=50. Never filters.
holder_labels(mint, pool) -> dict(dev_pct, top10_pct, creator) ; pool / program-owned (PDA) holders excluded from top10.
Helius free RPC (key injected on the wire). ~6-10 credits per alert."""
import json, time, urllib.request, urllib.error, sys
RPC = "https://mainnet.helius-rpc.com/" + (("?api-key=" + __import__("os").environ["HELIUS_API_KEY"]) if __import__("os").environ.get("HELIUS_API_KEY") else "")  # Hark injects the key on the wire; GitHub Actions passes HELIUS_API_KEY
SYS = "11111111111111111111111111111111"

def rpc(m, p):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p}).encode()
    for t in range(3):
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(RPC, body, {"Content-Type": "application/json"}), timeout=25))
            if "error" in r:
                if r["error"].get("code") in (-32429, 429): time.sleep(2 * (t + 1)); continue
                return None
            return r["result"]
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(2 * (t + 1)); continue
            return None
        except Exception:
            time.sleep(1)
    return None

def creator_of(mint):
    before = oldest = None
    for _ in range(6):
        p = {"limit": 1000}
        if before: p["before"] = before
        sg = rpc("getSignaturesForAddress", [mint, p]) or []
        if not sg: break
        oldest = before = sg[-1]["signature"]
        if len(sg) < 1000: break
    if not oldest: return None
    tx = rpc("getTransaction", [oldest, {"encoding": "json", "maxSupportedTransactionVersion": 0}])
    return tx["transaction"]["message"]["accountKeys"][0] if tx else None

def holder_labels(mint, pool=None):
    out = {"dev_pct": None, "top10_pct": None, "creator": None}
    sup = rpc("getTokenSupply", [mint])
    total = float(sup["value"]["uiAmount"] or 0) if sup else 0
    if not total: return out
    big = (rpc("getTokenLargestAccounts", [mint]) or {}).get("value", [])
    if big:
        accs = rpc("getMultipleAccounts", [[b["address"] for b in big], {"encoding": "jsonParsed"}]) or {"value": []}
        owners = []
        for b, a in zip(big, accs["value"]):
            try: owners.append(a["data"]["parsed"]["info"]["owner"])
            except Exception: owners.append(None)
        uniq = [o for o in set(owners) if o]
        oinfo = rpc("getMultipleAccounts", [uniq, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}]) or {"value": []}
        prog = {o: (i or {}).get("owner") for o, i in zip(uniq, oinfo["value"])}
        # keep wallets: owner account is System-owned or doesn't exist yet; drop pool/curve PDAs and program accounts
        held = [float(b["uiAmount"] or 0) for b, o in zip(big, owners)
                if o and o != pool and prog.get(o) in (SYS, None) and o not in ("", None)]
        out["top10_pct"] = round(100 * sum(sorted(held, reverse=True)[:10]) / total, 1)
    c = creator_of(mint)
    out["creator"] = c
    if c:
        r = rpc("getTokenAccountsByOwner", [c, {"mint": mint}, {"encoding": "jsonParsed"}]) or {"value": []}
        bal = sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in r["value"])
        out["dev_pct"] = round(100 * bal / total, 2)
    return out

if __name__ == "__main__":
    print(json.dumps(holder_labels(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)))
