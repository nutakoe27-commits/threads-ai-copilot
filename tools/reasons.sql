-- Раскладка причин отказа: по ней видно, какой критерий режет сильнее всего
-- и не пора ли его смягчить. Стоит ноль запросов к API.
.mode column
.headers on
.width 60 6
SELECT icp_reason, COUNT(*) AS cnt
FROM companies WHERE icp_status = 'rejected'
GROUP BY icp_reason ORDER BY cnt DESC LIMIT 25;
