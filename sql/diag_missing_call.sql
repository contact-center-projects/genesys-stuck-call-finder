/* =====================================================================
   diag_missing_call.sql - why did v4 miss a call CCPulse shows waiting?
   Fill in ONE of the two variables below (the ANI/phone number from
   CCPulse, or a CALLID if you already have one), then run the whole
   script as one batch - no special permissions needed.
   ===================================================================== */
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SET NOCOUNT ON;

DECLARE @ani    varchar(50)  = NULL;   -- e.g. '0501234567' - fill in ONE of
DECLARE @callid varchar(50)  = NULL;   -- e.g. '0000123456' - these two
DECLARE @queue  varchar(255) = N'LeumiCall_SECTOR_1';  -- the VQ/RP you searched for

IF @callid IS NULL AND @ani IS NOT NULL
    SELECT TOP 1 @callid = CALLID FROM G_CALL_ACTIVE WHERE CALLANI = @ani ORDER BY CREATED DESC;

SELECT @callid AS ResolvedCallId;

-- 1) The call's open party rows right now - this is where WaitLegCreated
--    comes from (MAX CREATED among agent-reserved / ACD-queue rows).
SELECT PARTYID, TYPE, PROLE, ENDPOINTTYPE, ENDPOINTDN, AGENTID, STATE,
       CREATED, TERMINATED
FROM G_PARTY
WHERE CALLID = @callid
ORDER BY CREATED;

-- 2) EVERY history row for this call matching the queue value, no time
--    window at all - this shows the REAL tag time(s), so we can see how
--    far ADDED actually sits from WaitLegCreated (from query 1 above).
SELECT KEYNAME, VALUE, ADDED
FROM G_USERDATA_HISTORY
WHERE CALLID = @callid
  AND VALUE = @queue
ORDER BY ADDED;

-- 3) For context: the call's full attach-data timeline (capped to 200
--    rows) - shows whether VQ_Name/RP gets re-stamped on each requeue,
--    or was only ever written once near the start.
SELECT TOP 200 KEYNAME, VALUE, ADDED
FROM G_USERDATA_HISTORY
WHERE CALLID = @callid
ORDER BY ADDED;
