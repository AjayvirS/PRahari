ALTER TABLE review_jobs ADD COLUMN comment_id TEXT;
ALTER TABLE review_jobs ADD COLUMN comment_body TEXT;
ALTER TABLE review_jobs ADD COLUMN comment_author TEXT;
ALTER TABLE review_jobs ADD COLUMN comment_type TEXT;
ALTER TABLE review_jobs ADD COLUMN in_reply_to_id TEXT;

DROP INDEX IF EXISTS idx_review_jobs_dedup;

CREATE UNIQUE INDEX IF NOT EXISTS idx_review_jobs_dedup
ON review_jobs (job_type, repo, pr_number, head_sha)
WHERE job_type = 'review_pr';

CREATE UNIQUE INDEX IF NOT EXISTS idx_review_jobs_comment_dedup
ON review_jobs (job_type, repo, pr_number, comment_id);
