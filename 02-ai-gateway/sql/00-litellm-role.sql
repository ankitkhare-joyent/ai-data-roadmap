-- The gateway's database user. The password here is a throwaway: bao-bootstrap registers "litellm" as an
-- OpenBao static role, and OpenBao immediately sets a random password that only it (and the gateway) know.
CREATE ROLE litellm LOGIN PASSWORD 'replaced-by-openbao';
ALTER DATABASE litellm OWNER TO litellm;
