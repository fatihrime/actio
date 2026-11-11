-- SPEND: take one token from every bucket, if every bucket has one. All or nothing.
--
-- KEYS: one per bucket.
-- ARGV: now (ms), then for each bucket its size, and the time (ms) one token takes to
--       come back.
-- Reply: 0 when a token was taken from each. Otherwise the time (ms) until every bucket
--        has one, and nothing was taken.
--
-- A bucket is stored as one number: the moment it will be full again. Taking a token
-- moves that moment one interval later. A bucket of `size` tokens is empty when that
-- moment is `size` intervals away, so a token may be taken while it is nearer than that.
-- A full bucket needs no key: each key expires at the moment it holds.
--
-- Numbers are stored with string.format('%d'), never tostring(), which keeps only 14
-- significant digits.
local now = tonumber(ARGV[1])
local wait = 0
local full_at = {}
local corrected = {}

local function store(key, at)
  redis.call('SET', key, string.format('%d', at), 'PX', string.format('%d', at - now))
end

for i, key in ipairs(KEYS) do
  local size = tonumber(ARGV[2 * i])
  local interval = tonumber(ARGV[2 * i + 1])
  local empty_for = size * interval

  local at = tonumber(redis.call('GET', key)) or now
  if at < now then at = now end
  -- A stored moment further away than an empty bucket's can't come from this limit and
  -- this clock: the limit was loosened since, or a caller's clock ran ahead. Treat the
  -- bucket as empty, and remember to store that even if this attempt is refused.
  -- Otherwise it would stay closed until the stored moment came near.
  if at > now + empty_for then
    at = now + empty_for
    corrected[i] = at
  end

  at = at + interval
  local short_for = at - empty_for - now
  if short_for > wait then wait = short_for end
  full_at[i] = at
end

if wait > 0 then
  for i, key in ipairs(KEYS) do
    if corrected[i] then store(key, corrected[i]) end
  end
  return wait
end

for i, key in ipairs(KEYS) do store(key, full_at[i]) end
return 0
