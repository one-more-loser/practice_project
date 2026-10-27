-- One restricted, read-only role. It is NOT a superuser, cannot bypass RLS,
-- cannot create objects, and is used only through SET ROLE by the evaluator.
DROP ROLE IF EXISTS csq_agent_query;
CREATE ROLE csq_agent_query NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOINHERIT NOBYPASSRLS;
