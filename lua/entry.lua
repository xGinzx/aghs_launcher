-- Local entry point. The user's Workshop code is stored as ahb_workshop.lua.
-- SPDX-License-Identifier: MIT
local http_bridge = require('ahb_bridge')
http_bridge.install()
local ui_transport = require('ahb_ui_transport')
ui_transport.install()

local workshop_result = require('ahb_workshop')
local activate_workshop = Activate
local precache_workshop = Precache
local spawn_group_precache_workshop = SpawnGroupPrecache
local output = print_Engine or print

local function precache_finished(...)
    output('[AGHS_ASSETS] workshop Precache completed')
    return ...
end

-- Dota discovers lifecycle hooks on the entry script's own scope. A function
-- inherited from a required module is not sufficient; export it here too.
function Precache(...)
    if type(precache_workshop) == 'function' then
        output('[AGHS_ASSETS] workshop Precache started')
        return precache_finished(precache_workshop(...))
    end
end

-- Dynamically loaded encounter rooms have their own precache context. Dota
-- looks up this hook on the entry scope just like the initial Precache hook.
function SpawnGroupPrecache(...)
    if type(spawn_group_precache_workshop) == 'function' then
        output('[AGHS_ASSETS] workshop SpawnGroupPrecache dispatched')
        return spawn_group_precache_workshop(...)
    end
end

local function activation_finished(...)
    http_bridge.attach()
    ui_transport.attach()
    http_bridge.probe()
    return ...
end

function Activate(...)
    http_bridge.attach()
    if type(activate_workshop) == 'function' then
        return activation_finished(activate_workshop(...))
    end
    return activation_finished()
end

return workshop_result
