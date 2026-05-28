-- Coverage analysis: how many indian_brand drug_id_1mg records yield dosing results,
-- broken down by match_combination and which path (primary vs fallback) returns them.

WITH

brands AS (
    SELECT DISTINCT ON (drug_id_1mg)
        drug_id_1mg,
        match_combination,
        rxcui
    FROM drugdb.indian_brand
    ORDER BY drug_id_1mg
),

-- PRIMARY path: rxcui -> drug -> dosing_regimen
-- (excludes drugbank / us_unapproved as the service does)
primary_hits AS (
    SELECT DISTINCT b.drug_id_1mg
    FROM brands b
    JOIN drugdb.drug d
        ON d.rxcui = ANY(b.rxcui)
    JOIN drugdb.dosing_regimen dr
        ON dr.formulation_id = d.formulation_id
    WHERE b.match_combination NOT IN ('drugbank', 'us_unapproved')
      AND dr.renal_function   = 'any'
      AND dr.hepatic_function = 'any'
      AND dr.pregnancy_status = 'any'
      AND dr.dose_basis       = 'fixed'
      AND dr.frequency        IS NOT NULL
      AND UPPER(COALESCE(dr.dose_amount, '')) != 'CONTRAINDICATED'
),

-- FALLBACK path: unnest rxcui -> ingredients.unii -> DrugMasterLinkage -> drug -> dosing_regimen
-- uses set-based joins instead of correlated EXISTS for performance
brand_rxcui AS (
    SELECT drug_id_1mg, unnest(rxcui) AS rxcui_val
    FROM brands
),

fallback_hits AS (
    SELECT DISTINCT br.drug_id_1mg
    FROM brand_rxcui br
    JOIN drugdb.ingredients i
        ON i.rxcui = br.rxcui_val
       AND i.unii IS NOT NULL
    JOIN public."DrugMasterLinkage" dml
        ON i.unii = ANY(dml.unii_ids)
    JOIN drugdb.drug d
        ON d.master_linkage_id = dml.master_linkage_id
    JOIN drugdb.dosing_regimen dr
        ON dr.formulation_id = d.formulation_id
    WHERE dr.renal_function   = 'any'
      AND dr.hepatic_function = 'any'
      AND dr.pregnancy_status = 'any'
      AND dr.dose_basis       = 'fixed'
      AND dr.frequency        IS NOT NULL
      AND UPPER(COALESCE(dr.dose_amount, '')) != 'CONTRAINDICATED'
)

SELECT
    b.match_combination,
    COUNT(*)                                                                    AS total_drugs,
    COUNT(ph.drug_id_1mg)                                                       AS primary_path_hits,
    COUNT(fh.drug_id_1mg)                                                       AS fallback_path_hits,
    COUNT(CASE WHEN ph.drug_id_1mg IS NOT NULL
                 OR fh.drug_id_1mg IS NOT NULL THEN 1 END)                      AS either_path_hits,
    COUNT(CASE WHEN ph.drug_id_1mg IS NULL
                AND fh.drug_id_1mg IS NULL     THEN 1 END)                      AS no_results,
    ROUND(
        100.0
        * COUNT(CASE WHEN ph.drug_id_1mg IS NOT NULL
                       OR fh.drug_id_1mg IS NOT NULL THEN 1 END)
        / NULLIF(COUNT(*), 0),
        1
    )                                                                           AS coverage_pct
FROM brands b
LEFT JOIN primary_hits  ph ON ph.drug_id_1mg = b.drug_id_1mg
LEFT JOIN fallback_hits fh ON fh.drug_id_1mg = b.drug_id_1mg
GROUP BY b.match_combination
ORDER BY total_drugs DESC;
