-- COLLECT: release up to GC_BATCH expired reservations, as RESERVE does first.
-- ARGV: now. Reply: how many expired.
if not has_counters() then return 0 end
local count = collect(tonumber(ARGV[1]))
touch()
return count
