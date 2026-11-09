-- Shared by every quota script. Python prepends the constants WARN, GC_BATCH, DONE_TTL
-- and IDLE_TTL, so they are defined once, in actio_quota.model.
--
-- KEYS, the same six for every script, all under the user's hash tag {u:<id>}:
--   [1] meta      HASH  epoch                       (missing: counters need a rebuild)
--   [2] buckets   HASH  <name>/start, /reset, /limit, /used, /reserved, /warned
--   [3] rsv       HASH  rid -> grant: "credits|units|name|start|name|start..."
--   [4] rsv_exp   ZSET  rid scored by expiry (ms)
--   [5] done      HASH  rid -> "outcome|" .. grant
--   [6] done_exp  ZSET  rid scored by when the done marker is forgotten (ms)
--
-- Numbers: Lua 5.1 numbers are doubles, exact for integers below 2^53, and Python keeps
-- every value below that. They are stored with S(), never tostring(): tostring() keeps
-- only 14 significant digits.

local MICRO = 1000000

local function S(n)
  return string.format('%d', n)
end

-- floor(a / b) for integers 0 <= a, 0 < b. Below 2^53 a correctly rounded division
-- already floors exactly, so the loops never run for the values Python sends. They make
-- the result exact anyway, should a value ever slip past that bound.
local function idiv(a, b)
  local q = math.floor(a / b)
  while q * b > a do q = q - 1 end
  while (q + 1) * b <= a do q = q + 1 end
  return q
end

local function cdiv(a, b)
  local q = idiv(a, b)
  if q * b < a then q = q + 1 end
  return q
end

local function split(s)
  local parts = {}
  for part in string.gmatch(s, '[^|]+') do parts[#parts + 1] = part end
  return parts
end

local function has_counters()
  return redis.call('EXISTS', KEYS[1]) == 1
end

-- Every write pushes back the idle expiry of all of the user's keys together. A user
-- idle for longer loses them all at once, and is rebuilt from the ledger next time.
local function touch()
  for i = 1, 6 do redis.call('PEXPIRE', KEYS[i], IDLE_TTL) end
end

local function load_bucket(name)
  local v = redis.call('HMGET', KEYS[2], name .. '/start', name .. '/reset', name .. '/limit',
    name .. '/used', name .. '/reserved', name .. '/warned')
  if not v[1] then return nil end
  return { start = tonumber(v[1]), reset = tonumber(v[2]), limit = tonumber(v[3]),
    used = tonumber(v[4]), reserved = tonumber(v[5]), warned = v[6] }
end

local function save_bucket(name, b)
  redis.call('HSET', KEYS[2], name .. '/start', S(b.start), name .. '/reset', S(b.reset),
    name .. '/limit', S(b.limit), name .. '/used', S(b.used), name .. '/reserved', S(b.reserved),
    name .. '/warned', b.warned)
end

-- Mark each threshold the bucket has newly crossed; append name, percent to `out`.
local function warn(name, b, out)
  if b.limit == 0 then return end
  for _, percent in ipairs(WARN) do
    local seen = string.find(',' .. b.warned .. ',', ',' .. percent .. ',', 1, true)
    if not seen and b.used * 100 >= b.limit * percent then
      if b.warned == '' then b.warned = S(percent) else b.warned = b.warned .. ',' .. S(percent) end
      out[#out + 1] = name
      out[#out + 1] = percent
    end
  end
end

-- End a live reservation: give back its held credits in every window that admitted it
-- and is still current, charge `credits` if settled, and leave a done marker.
local function finish(rid, outcome, credits, now)
  local grant = redis.call('HGET', KEYS[3], rid)
  redis.call('HDEL', KEYS[3], rid)
  redis.call('ZREM', KEYS[4], rid)
  local g = split(grant)
  local held = tonumber(g[1])
  local warnings = {}
  for i = 3, #g, 2 do
    local name, start = g[i], tonumber(g[i + 1])
    local b = load_bucket(name)
    if b and b.start == start then
      b.reserved = b.reserved - held
      if outcome == 'settled' then
        b.used = b.used + credits
        warn(name, b, warnings)
      end
      save_bucket(name, b)
    end
  end
  redis.call('HSET', KEYS[5], rid, outcome .. '|' .. grant)
  redis.call('ZADD', KEYS[6], S(now + DONE_TTL), rid)
  return warnings
end

-- Release up to GC_BATCH expired reservations, oldest expiry first (ties by id), and
-- forget up to GC_BATCH stale done markers. Returns how many reservations expired.
local function collect(now)
  local expired = redis.call('ZRANGE', KEYS[4], '-inf', S(now), 'BYSCORE', 'LIMIT', 0, GC_BATCH)
  for _, rid in ipairs(expired) do finish(rid, 'expired', 0, now) end
  local stale = redis.call('ZRANGE', KEYS[6], '-inf', S(now), 'BYSCORE', 'LIMIT', 0, GC_BATCH)
  for _, rid in ipairs(stale) do
    redis.call('HDEL', KEYS[5], rid)
    redis.call('ZREM', KEYS[6], rid)
  end
  return #expired
end

-- A live done marker's outcome and grant, or nil if there is none or it has expired.
local function done_marker(rid, now)
  local d = redis.call('HGET', KEYS[5], rid)
  if not d then return nil end
  if now >= tonumber(redis.call('ZSCORE', KEYS[6], rid)) then return nil end
  local bar = string.find(d, '|', 1, true)
  return string.sub(d, 1, bar - 1), string.sub(d, bar + 1)
end

local function grant_reply(grant)
  local reply = { 'granted' }
  for _, part in ipairs(split(grant)) do reply[#reply + 1] = part end
  return reply
end
