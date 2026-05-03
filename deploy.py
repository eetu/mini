from pyinfra import local

local.include("tasks/bootstrap.py")
local.include("tasks/ssh.py")
local.include("tasks/firewall.py")
local.include("tasks/power.py")
local.include("tasks/autoupdate.py")
local.include("tasks/secrets.py")
local.include("tasks/caddy.py")
local.include("tasks/ollama.py")
