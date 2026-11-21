-- Local entry point. The user's Workshop code is stored as ahb_workshop.lua.
-- SPDX-License-Identifier: MIT
local http_bridge = require('ahb_bridge')
http_bridge.install()

local workshop_result = require('ahb_workshop')
local activate_workshop = Activate

local function activation_finished(...)
    http_bridge.attach()
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
