-- Bounded delivery of custom UI events. SPDX-License-Identifier: MIT
local Transport = {}
local state = { queues = {}, order = {}, sequence = 0, queued = 0 }
local output = print_Engine or print
local MAX_EVENT = 4 * 1024 * 1024
local MAX_QUEUE = 32 * 1024 * 1024
local SMALL_EVENT = 2048
local CHUNK = 512
local snapshots = {
    GetPlayerRelicData_feedback = true,
    GetPlayerSkillCoreData_feedback = true,
    GetPlayerWorldConnectorData_feedback = true,
    GetPlayerItemData_feedback = true,
    GetPlayerInfo_feedback = true,
    GetPlayerPrivilegeData_feedback = true,
    GetPlayerSevenDayLoginRecordsData_feedback = true,
    GetPlayerParticleList_feedback = true,
    GetPlayerParticleSetting_feedback = true,
    GetPlayerFavoriteArtifactList_feedback = true
}

local function quote(value)
    return '"' .. value:gsub('[%z\1-\31\\"]', function(character)
        if character == '"' then return '\\"' end
        if character == '\\' then return '\\\\' end
        return string.format('\\u%04x', character:byte())
    end) .. '"'
end

-- Native event tables arrive as JS objects, including numeric Lua keys.
-- Keep that representation when a large event travels through JSON.
local function encode(value, seen, depth)
    local kind = type(value)
    if kind == 'string' then return quote(value), value end
    if kind == 'number' then
        assert(value == value and value ~= math.huge and value ~= -math.huge, 'Non-finite UI value')
        return string.format('%.17g', value), value
    end
    if kind == 'boolean' then return value and 'true' or 'false', value end
    if value == nil then return 'null', nil end
    assert(kind == 'table' and depth <= 32 and not seen[value], 'Unsupported or cyclic UI data')
    seen[value] = true
    local parts, snapshot, keys = {}, {}, {}
    for key in pairs(value) do
        assert(type(key) == 'string' or type(key) == 'number', 'Unsupported UI key')
        keys[#keys + 1] = key
    end
    table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
    for _, key in ipairs(keys) do
        local item = value[key]
        local text, copied = encode(item, seen, depth + 1)
        parts[#parts + 1] = quote(tostring(key)) .. ':' .. text
        snapshot[key] = copied
    end
    seen[value] = nil
    return '{' .. table.concat(parts, ',') .. '}', snapshot
end

local function hex(data)
    local result = {}
    for index = 1, #data do result[index] = string.format('%02x', data:byte(index)) end
    return table.concat(result)
end

local function valid_player(player)
    return player ~= nil and (not player.IsNull or not player:IsNull())
end

local function queue_for(player)
    local queue = state.queues[player]
    if not queue then
        queue = { topics = {}, order = {}, cursor = 1 }
        state.queues[player] = queue
        state.order[#state.order + 1] = player
    end
    return queue
end

function Transport.attach()
    if state.scheduled or state.queued == 0 or not GameRules then return end
    local entity = GameRules:GetGameModeEntity()
    if entity then
        state.scheduled = true
        entity:SetContextThink('ahb_ui_delivery', Transport.pump, 0)
    end
end

function Transport.send(player, name, data)
    if not valid_player(player) then return end
    local ok, text, snapshot = pcall(encode, data, {}, 0)
    if not ok then
        output('[AGHS_UI] unsupported event value: ' .. tostring(name))
        return state.send(state.manager, player, name, data)
    end
    local queue = queue_for(player)
    local topic = queue.topics[name]
    if not topic then
        topic = { entries = {}, first = 1, last = 0 }
        queue.topics[name] = topic
        queue.order[#queue.order + 1] = name
    end
    -- Full-state notifications can replace a not-yet-started notification of
    -- the same kind. Keep an in-flight transfer intact, and keep one pending
    -- copy so contexts subscribing mid-transfer can still receive every part.
    local replacing
    if snapshots[name] and topic.last >= topic.first then
        local latest = topic.entries[topic.last]
        if latest and latest.offset == 1 then
            if latest.text == text then return true end
            replacing = latest
        end
    end
    local previous_bytes = replacing and (replacing.bytes * 2 + 512) or 0
    if #text > MAX_EVENT or state.queued - previous_bytes + #text * 2 + 512 > MAX_QUEUE then
        if topic.first > topic.last then
            queue.topics[name] = nil
            for index = #queue.order, 1, -1 do
                if queue.order[index] == name then table.remove(queue.order, index) end
            end
        end
        output('[AGHS_UI] event exceeds bounded transfer limit: ' .. tostring(name))
        return false
    end
    local entry = { name = name, text = text, bytes = #text, snapshot = snapshot, offset = 1 }
    if #text > SMALL_EVENT then
        state.sequence = state.sequence + 1
        entry.id = state.boot .. '_' .. string.format('%x', state.sequence)
        entry.total = math.ceil(#text / CHUNK)
        entry.snapshot = nil
        output('[AGHS_UI] chunking ' .. tostring(name) .. ' bytes=' .. #text .. ' parts=' .. entry.total)
    end
    if replacing then
        topic.entries[topic.last] = entry
    else
        topic.last = topic.last + 1
        topic.entries[topic.last] = entry
    end
    state.queued = state.queued - previous_bytes + entry.bytes * 2 + 512
    Transport.attach()
end

function Transport.pump()
    local remaining = 16384
    local next_order = {}
    for _, player in ipairs(state.order) do
        local queue = state.queues[player]
        local allowance = math.min(8192, remaining)
        local skipped = 0
        while queue and #queue.order > 0 and allowance > 0 do
            if queue.cursor > #queue.order then queue.cursor = 1 end
            local name = queue.order[queue.cursor]
            local topic = queue.topics[name]
            local entry = topic.entries[topic.first]
            local finished = false
            if not valid_player(player) then
                finished = true
            else
                local cost = entry.id and (CHUNK * 2 + 256) or (entry.bytes * 2 + 256)
                if cost > allowance then
                    skipped = skipped + 1
                    if skipped >= #queue.order then break end
                elseif entry.id then
                    local part = math.floor((entry.offset - 1) / CHUNK) + 1
                    state.send(state.manager, player, 'ahb_ui_fragment', {
                        id = entry.id, event = entry.name, part = part, total = entry.total,
                        bytes = entry.bytes, hex = hex(entry.text:sub(entry.offset, entry.offset + CHUNK - 1))
                    })
                    entry.offset = entry.offset + CHUNK
                    allowance, remaining = allowance - cost, remaining - cost
                    skipped = 0
                else
                    state.send(state.manager, player, entry.name, entry.snapshot)
                    entry.offset = entry.bytes + 1
                    allowance, remaining = allowance - cost, remaining - cost
                    skipped = 0
                end
                if entry.offset > entry.bytes then
                    finished = true
                end
            end
            if finished then
                state.queued = state.queued - entry.bytes * 2 - 512
                topic.entries[topic.first] = nil
                topic.first = topic.first + 1
            end
            if topic.first > topic.last then
                queue.topics[name] = nil
                table.remove(queue.order, queue.cursor)
            else
                queue.cursor = queue.cursor + 1
            end
        end
        if queue and #queue.order > 0 then
            next_order[#next_order + 1] = player
        else
            state.queues[player] = nil
        end
    end
    state.order = next_order
    if #next_order > 0 then
        if #next_order > 1 then table.insert(next_order, table.remove(next_order, 1)) end
        return 0.02
    end
    state.scheduled = false
end

function Transport.install()
    if state.manager or (IsServer and not IsServer()) then return end
    local manager = CustomGameEventManager
    if not manager then return end
    state.manager, state.send = manager, manager.Send_ServerToPlayer
    state.boot = 'u' .. string.format('%x', math.floor((RealTime or Time)() * 1000000))
    local facade = {}
    setmetatable(facade, { __index = function(_, key)
        local value = manager[key]
        if type(value) == 'function' then
            local bound = function(_, ...) return value(manager, ...) end
            rawset(facade, key, bound)
            return bound
        end
        return value
    end })
    function facade:Send_ServerToPlayer(player, name, data)
        return Transport.send(player, name, data)
    end
    function facade:Send_ServerToAllClients(name, data)
        if not PlayerResource then return manager:Send_ServerToAllClients(name, data) end
        for player_id = 0, 63 do
            local player = PlayerResource:GetPlayer(player_id)
            if valid_player(player) then Transport.send(player, name, data) end
        end
    end
    function facade:Send_ServerToTeam(team, name, data)
        if not PlayerResource then return manager:Send_ServerToTeam(team, name, data) end
        for player_id = 0, 63 do
            local player = PlayerResource:GetPlayer(player_id)
            if valid_player(player) and player:GetTeamNumber() == team then Transport.send(player, name, data) end
        end
    end
    _G.CustomGameEventManager = facade
    output('[AGHS_UI] bounded event delivery installed')
end

return Transport
