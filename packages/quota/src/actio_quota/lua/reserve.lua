-- RESERVE: admit a request if it fits in every bucket, and hold its credits.
-- ARGV: now, rid, ttl, base_micro, unit_micro, min_units, max_units, bucket count, then
--       per bucket: name, limit, kind ('r' rolling | 'f' fixed), a, b
--       (rolling: a = duration; fixed: a = start, b = end).
-- Reply: {'needs_rebuild'} | {'granted', credits, units, name, start, ...}
--      | {'denied', retry_at (-1: never), name, ...}
local now, rid, ttl = tonumber(ARGV[1]), ARGV[2], tonumber(ARGV[3])
local base, unit = tonumber(ARGV[4]), tonumber(ARGV[5])
local min_units, max_units = tonumber(ARGV[6]), tonumber(ARGV[7])

if not has_counters() then return { 'needs_rebuild' } end

-- 1. A retry of an admitted request gets its original grant back.
local live = redis.call('HGET', KEYS[3], rid)
if live then return grant_reply(live) end
local _, finished = done_marker(rid, now)
if finished then return grant_reply(finished) end

-- 2. Release expired reservations.
collect(now)

-- 3. Each bucket's current window: a past window counts as closed.
local current = {}
local i = 9
for n = 1, tonumber(ARGV[8]) do
  local c = { name = ARGV[i], limit = tonumber(ARGV[i + 1]), kind = ARGV[i + 2],
    a = tonumber(ARGV[i + 3]), b = tonumber(ARGV[i + 4]), used = 0, reserved = 0 }
  i = i + 5
  local stored = load_bucket(c.name)
  if c.kind == 'r' then
    if stored and now < stored.reset then c.state = stored end
  else
    if stored and stored.start == c.a then c.state = stored else c.start, c.reset = c.a, c.b end
  end
  if c.state then
    c.used, c.reserved = c.state.used, c.state.reserved
    c.start, c.reset = c.state.start, c.state.reset
  end
  current[n] = c
end

-- 4. Deny if even the smallest allowed request doesn't fit. A free request always fits.
local smallest = cdiv(base + min_units * unit, MICRO)
local short, never, retry_at = {}, false, 0
for _, c in ipairs(current) do
  if smallest > 0 and c.used + c.reserved + smallest > c.limit then
    short[#short + 1] = c.name
    if smallest > c.limit then never = true end
    if (c.reset or 0) > retry_at then retry_at = c.reset end
  end
end
if #short > 0 then
  local reply = { 'denied', never and -1 or retry_at }
  for _, name in ipairs(short) do reply[#reply + 1] = name end
  return reply
end

-- 5. Grant the most units that fit every bucket.
local units = max_units
if #current > 0 and unit > 0 then
  local headroom = nil
  for _, c in ipairs(current) do
    local room = c.limit - c.used - c.reserved
    if headroom == nil or room < headroom then headroom = room end
  end
  local room = headroom * MICRO - base
  if room < 0 then
    units = min_units
  elseif room < max_units * unit then
    units = math.max(min_units, idiv(room, unit))
  end
end
local credits = cdiv(base + units * unit, MICRO)

-- 6. Open closed windows, hold the credits, and record the reservation.
local grant = S(credits) .. '|' .. S(units)
for _, c in ipairs(current) do
  local b = c.state
  if not b then
    if c.kind == 'r' then
      b = { start = now, reset = now + c.a }
    else
      b = { start = c.a, reset = c.b }
    end
    b.used, b.reserved, b.warned = 0, 0, ''
  end
  b.limit = c.limit
  b.reserved = b.reserved + credits
  save_bucket(c.name, b)
  grant = grant .. '|' .. c.name .. '|' .. S(b.start)
end
redis.call('HSET', KEYS[3], rid, grant)
redis.call('ZADD', KEYS[4], S(now + ttl), rid)
touch()
return grant_reply(grant)
