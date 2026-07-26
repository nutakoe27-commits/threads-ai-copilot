-- Забыть результаты обхода сайтов, чтобы --stage dossier перечитал их заново.
--
-- Когда это нужно: изменился config/prompts/classify.md — например, добавилось
-- требование собирать конкретные факты. Уже обойдённые компании помечены
-- site_checked_at и второй раз не обходятся, поэтому отметку надо снять.
--
-- Что НЕ трогается: сами компании и вердикты по ICP. Обход сайтов и
-- классификация не стоят ни одного запроса к Checko — только к модели,
-- и это копейки. Лимит 100 запросов в сутки не расходуется вовсе.
--
-- Письма стираются заодно: они собраны из старого досье, и оставлять их
-- рядом с новым бессмысленно.
--
--   sqlite3 data/leads.db < tools/reset_dossier.sql

UPDATE companies
SET site_type = NULL,
    site_confidence = NULL,
    site_summary = NULL,
    site_specialization = NULL,
    site_facts = NULL,
    site_error = NULL,
    site_checked_at = NULL,
    letter_subject = NULL,
    letter_body = NULL,
    letter_why = NULL,
    letter_facts = NULL,
    letter_status = NULL,
    letter_written_at = NULL,
    last_reported = NULL
WHERE site_checked_at IS NOT NULL;

SELECT changes() AS 'Компаний к повторному обходу';
