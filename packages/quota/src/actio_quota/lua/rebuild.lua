-- REBUILD: replace the user's counters with states Python computed from the ledger.
-- Applies only if the epoch is still ARGV[1] ('' means: no counters), so only one of
-- two racing rebuilds wins.
-- ARGV: if_epoch, bucket count, then per bucket: name, start, reset, limit, used, warned.
-- Reply: 1 if applied, else 0.
local epoch = redis.call('HGET', KEYS[1], 'epoch')
if (epoch or '') ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5], KEYS[6])
redis.call('HSET', KEYS[1], 'epoch', S((tonumber(epoch) or 0) + 1))
local i = 3
for _ = 1, tonumber(ARGV[2]) do
  save_bucket(ARGV[i], { start = tonumber(ARGV[i + 1]), reset = tonumber(ARGV[i + 2]),
    limit = tonumber(ARGV[i + 3]), used = tonumber(ARGV[i + 4]), reserved = 0,
    warned = ARGV[i + 5] })
  i = i + 6
end
touch()
return 1
