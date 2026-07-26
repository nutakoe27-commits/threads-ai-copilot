-- Стереть все сгенерированные письма, чтобы --stage compose написал их заново.
--
-- Когда это нужно: вы поправили config/prompts/letter.md или углы в
-- config/prompts/angles/. Уже написанные письма от этого не меняются —
-- они лежат в базе готовым текстом.
--
-- Что НЕ трогается: сами компании, вердикты по ICP, классификация по сайту.
-- Стираются только письма, так что повторный compose не стоит ни одного
-- запроса к Checko — только к модели.
--
-- ВНИМАНИЕ: компании, уже попавшие в утренний список, после этого туда
-- вернутся с новым письмом. Если этого не нужно, уберите вторую строку
-- с last_reported.
--
--   sqlite3 data/leads.db < tools/reset_letters.sql

UPDATE companies
SET letter_subject = NULL,
    letter_body = NULL,
    letter_why = NULL,
    letter_facts = NULL,
    letter_status = NULL,
    letter_written_at = NULL,
    last_reported = NULL
WHERE letter_written_at IS NOT NULL;

SELECT changes() AS 'Писем стёрто';
