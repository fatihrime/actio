-- LOOK or ADD: what a provider's plan lets this installation send to a model, counted in
-- buckets of requests or tokens that refill steadily (budget.py).
--
-- KEYS: one per bucket.
-- ARGV: 'look' or 'add', now (ms), then for each bucket its size, its period (ms): how
--       long an empty one takes to be full again, and an amount.
-- Reply: one number for each bucket.
--   'look': the time (ms) until its amount fits, 0 when it fits now. An amount larger
--           than the bucket's size fits a full one. Nothing is written, not even a
--           correction.
--   'add':  0. The amount was taken, or given back when it is negative: never refused,
--           and the bucket goes no lower than empty and no higher than full.
--
-- A bucket is stored as one number, the moment it will be full again, as the rate
-- limiter's is (rate_limit.lua), and its key expires at that moment: a full bucket takes
-- no key. It is empty while that moment is a period away. An amount moves the moment by
-- amount * period / size milliseconds, rounded down, whether it is taken or given back:
-- rounded up, a bucket would lose the last request of a burst.
--
-- Numbers are stored with string.format('%d'), never tostring(), which keeps only 14
-- significant digits.
local mode = ARGV[1]
if mode ~= 'look' and mode ~= 'add' then
  return redis.error_reply('budget.lua: the operation is look or add')
end
local now = tonumber(ARGV[2])
local out = {}

-- How far `amount` moves a bucket's moment, rounded down either way. More than the
-- bucket holds moves it as far as all of it does.
local function shift(amount, size, period)
  if amount > size then amount = size end
  if amount < -size then amount = -size end
  if amount >= 0 then
    return math.floor(amount * period / size)
  end
  return -math.floor(-amount * period / size)
end

for i, key in ipairs(KEYS) do
  local size = tonumber(ARGV[3 * i])
  local period = tonumber(ARGV[3 * i + 1])
  local amount = tonumber(ARGV[3 * i + 2])
  local empty_at = now + period

  local at = tonumber(redis.call('GET', key)) or now
  if at < now then at = now end
  -- A stored moment further away than an empty bucket's can't come from this limit and
  -- this clock: the limit was raised since, or a caller's clock ran ahead. The bucket is
  -- taken for empty, so that it isn't kept closed until that moment comes near.
  if at > empty_at then at = empty_at end

  if mode == 'look' then
    local short_for = at + shift(amount, size, period) - empty_at
    if short_for < 0 then short_for = 0 end
    out[i] = short_for
  else
    at = at + shift(amount, size, period)
    if at > empty_at then at = empty_at end
    if at <= now then
      redis.call('DEL', key)
    else
      redis.call('SET', key, string.format('%d', at), 'PX', string.format('%d', at - now))
    end
    out[i] = 0
  end
end

return out
