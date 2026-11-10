-- SETTLE or RELEASE a reservation.
-- ARGV: now, rid, outcome ('settled' | 'released'), credits (charged when settled)
-- Reply: {outcome, applied (1 | 0), name, percent, ...}: the warnings newly crossed.
local now, rid, outcome, credits = tonumber(ARGV[1]), ARGV[2], ARGV[3], tonumber(ARGV[4])

if not has_counters() then return { 'unknown', 0 } end
local warnings
if redis.call('HEXISTS', KEYS[3], rid) == 1 then
  warnings = finish_live(rid, outcome, credits, now)
else
  local previous, grant = done_marker(rid, now)
  if not previous then return { 'unknown', 0 } end
  if previous ~= 'expired' then return { previous, 0 } end
  -- Expiry gave back the hold, but the request ends only now: nothing is held.
  warnings = finish(rid, grant, 0, outcome, credits, now)
end
local reply = { outcome, 1 }
for _, w in ipairs(warnings) do reply[#reply + 1] = w end
touch()
return reply
