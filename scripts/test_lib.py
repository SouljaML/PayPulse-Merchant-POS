import json, hmac, hashlib

payload = {'provider_reference': 'MOCK-3BD4106824', 'result': 'SUCCESS'}
body = json.dumps(payload).encode()
sig = hmac.new(b'dev-secret', body, hashlib.sha256).hexdigest()

with open('/tmp/callback.json', 'wb') as f:
    f.write(body)

print('Signature:', sig)