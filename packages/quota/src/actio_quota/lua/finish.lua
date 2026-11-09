-- SETTLE or RELEASE a reservation.
-- ARGV: now, rid, outcome ('settled' | 'released'), credits (charged when settled)
-- Reply: {outcome, applied (1 | 0), name, percent, ...}: the warnings newly crossed.
local now, rid, outcome, credits = tonumber(ARGV[1]), ARGV[2], ARGV[3], tonumber(ARGV[4])

if not has_counters() then return { 'unknown', 0 } end
if redis.call('HEXISTS', KEYS[3], rid) == 1 then
  local reply = { outcome, 1 }
  for _, w in ipairs(finish(rid, outcome, credits, now)) do reply[#reply + 1] = w end
  touch()
  return reply
end
local previous = done_marker(rid, now)
if previous then return { previous, 0 } end
return { 'unknown', 0 }
