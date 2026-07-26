-- Отложенные письма: те, где фактов из досье набралось меньше минимума
-- (compose.min_facts в config/icp.yaml). В утренний список они не идут.
--
-- Смотреть сюда стоит, если отложенных вдруг стало много: обычно это значит
-- не что компании плохие, а что сайты плохо читаются и досье пустое.
-- Стоит ноль запросов к API.
--
--   sqlite3 data/leads.db < tools/thin.sql

.mode column
.headers on
.width 34 10 40 60

SELECT
    name,
    site_type,
    site_url,
    letter_facts
FROM companies
WHERE letter_status = 'thin'
ORDER BY letter_written_at DESC
LIMIT 40;

.print ''
.print 'Текст отложенных писем целиком:'
.print '  sqlite3 data/leads.db "SELECT name, letter_body FROM companies WHERE letter_status = ''thin''"'
