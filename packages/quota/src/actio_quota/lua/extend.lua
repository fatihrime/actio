-- EXTEND: keep a running request's reservation alive until now + ttl.
-- ARGV: now, rid, ttl. Reply: 1 if the reservation is live, else 0.
local now, rid, ttl = tonumber(ARGV[1]), ARGV[2], tonumber(ARGV[3])
if not has_counters() or redis.call('HEXISTS', KEYS[3], rid) == 0 then return 0 end
redis.call('ZADD', KEYS[4], S(now + ttl), rid)
touch()
return 1
