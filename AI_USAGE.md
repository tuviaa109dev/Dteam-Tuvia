## Tools I Used

I am used to using only Claude Code as an AI tool for application development, but when I saw the assignment
instructions I thought it would be a good opportunity to try combining other AI tools. So I decided to begin
with **GitHub Copilot** to get a skeleton of the project built, and then see what changes **Claude Code**
would offer.

## What Helped Most

I always like to ask the AI to get the basic structure built and running, though I don't fully count on the AI,
and I checked the Python in the Back-End to make sure it is structured correctly and logically. I also had the
AI create the automated tests and the Front-End without too much interference, because in my experience testing
and Front-End syntax are so straightforward that the chance of the AI getting them wrong is lower. It's also
easier to catch mistakes there by running the app on localhost and checking the test outputs.

## What I Had to Fix

At first, the AI generated the payload validation only in the worker, and I felt that validating that the
payload fits the job type should be the first step upon receiving a job (in the API). [Of course, the first
validation should be in the Front-End before sending the request, but that is beyond the scope of this
assignment.]

At first, the JSON logging was only done in the worker. I requested that the JSON formatter also be used in the
API, so that every stage of the job lifecycle (submission, cancellation, retries) produces structured logs with the
job's context, not only the worker.

## What AI Struggled With

The Dead Letter Queue was something the AI tools were not consistent about. At first, the AI created the DLQ
routes (POST, GET...) but then didn't find any reason to use them. In the Front-End there is an option to
filter out all the statuses except "failed" and the single source of truth is the DB, so there was no actual need
to move all the "failed" jobs to a separate queue, which made the routes useless. So I asked to remove them so
there wouldn't be useless code. But that left a DLQ that only duplicated the "failed" status, so I redesigned
it around the difference between two kinds of failure:

- **Failed (temporarily):** the data is fine, but something outside the job went wrong (a webhook was down, a
  worker crashed, a timeout). These jobs are retried with backoff and stay in the jobs list as "failed", where
  they can be retried again manually.
- **Dead letter (corrupted data):** the job can never succeed as it is (an unknown job type or a payload that
  fails validation). Retrying is pointless, so on the first such error the job is moved, with its full log
  history, out of the jobs table into a separate dead letter table.

The DLQ routes are back, now with a real purpose: in a separate "DLQ - dev" tab, a developer can inspect each
dead letter, fix its payload and requeue it (it returns to the jobs list under its original ID), or discard it.
Keeping these jobs in their own table also takes load off the main jobs list as the project scales. To
demonstrate it, the dev panel's "Fill" button also injects a few jobs with truly corrupted data, and there are
dedicated tests for this scenario.
