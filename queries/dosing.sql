WITH salt_ingredients AS (
  SELECT ib.salt_composition, ib.rxcui
  FROM drugdb.indian_brand ib
  WHERE ib.drug_id_1mg = $1
    AND ib.match_combination NOT IN ('drugbank', 'us_unapproved')
  LIMIT 1
),
candidate_formulations AS (
  SELECT
    d.formulation_id,
    d.rxcui,
    d.generic_name,
    d.has_dailymed,
    d.has_openfda,
    d.has_drugbank,
    d.has_rxnorm,
    COUNT(dr.id) AS dosing_row_count
  FROM drugdb.drug d
  JOIN salt_ingredients si ON d.rxcui = ANY(si.rxcui)
  LEFT JOIN drugdb.dosing_regimen dr ON dr.formulation_id = d.formulation_id
  GROUP BY
    d.formulation_id, d.rxcui, d.generic_name,
    d.has_dailymed, d.has_openfda, d.has_drugbank, d.has_rxnorm
),
best_formulation AS (
  SELECT DISTINCT ON (rxcui)
    formulation_id,
    rxcui,
    best_dose_basis
  FROM (
    SELECT
      cf.formulation_id,
      cf.rxcui,
      cf.has_dailymed,
      cf.has_openfda,
      cf.has_drugbank,
      cf.has_rxnorm,
      cf.dosing_row_count,
      MIN(
        CASE COALESCE(dr.dose_basis, '')
          WHEN 'fixed'    THEN 1
          WHEN 'per_kg'   THEN 2
          WHEN 'per_m2'   THEN 3
          WHEN 'titrated' THEN 4
          ELSE                 5
        END
      ) AS dose_basis_priority,
      (array_agg(
        dr.dose_basis
        ORDER BY
          CASE COALESCE(dr.dose_basis, '')
            WHEN 'fixed'    THEN 1
            WHEN 'per_kg'   THEN 2
            WHEN 'per_m2'   THEN 3
            WHEN 'titrated' THEN 4
            ELSE                 5
          END ASC
      ))[1] AS best_dose_basis
    FROM candidate_formulations cf
    JOIN drugdb.dosing_regimen dr ON dr.formulation_id = cf.formulation_id
    WHERE dr.age_group        = ANY($2::text[])
      AND dr.renal_function   = 'any'
      AND dr.hepatic_function = 'any'
      AND dr.pregnancy_status = 'any'
      AND dr.frequency        IS NOT NULL
      AND UPPER(COALESCE(dr.dose_amount, '')) != 'CONTRAINDICATED'
      AND (
        $2::text[] && ARRAY['pediatric','neonate','infant']
        OR dr.administration_notes NOT ILIKE '%pediatric%'
        OR dr.administration_notes IS NULL
      )
    GROUP BY
      cf.formulation_id, cf.rxcui,
      cf.has_dailymed, cf.has_openfda, cf.has_drugbank, cf.has_rxnorm,
      cf.dosing_row_count
  ) cf_ranked
  ORDER BY
    rxcui,
    dose_basis_priority ASC,
    CASE
      WHEN has_dailymed = true THEN 1
      WHEN has_openfda  = true THEN 2
      WHEN has_drugbank = true THEN 3
      WHEN has_rxnorm   = true THEN 4
      ELSE 5
    END ASC,
    dosing_row_count DESC,
    formulation_id ASC
),
ranked AS (
  SELECT
    dr.frequency,
    dr.route,
    dr.dose_amount,
    dr.dose_value,
    dr.dose_unit,
    dr.duration,
    dr.indication,
    dr.administration_notes,
    ROW_NUMBER() OVER (
      PARTITION BY
        dr.frequency,
        dr.route,
        dr.dose_value,
        dr.dose_unit,
        LOWER(COALESCE(dr.indication, ''))
      ORDER BY
        CASE
          WHEN dr.indication IS NOT NULL
           AND dr.administration_notes IS NOT NULL THEN 1
          WHEN dr.indication IS NOT NULL            THEN 2
          WHEN dr.administration_notes IS NOT NULL  THEN 3
          ELSE 4
        END ASC,
        dr.id ASC
    ) AS rn
  FROM best_formulation bf
  JOIN drugdb.dosing_regimen dr ON dr.formulation_id = bf.formulation_id
  WHERE dr.age_group        = ANY($2::text[])
    AND dr.renal_function   = 'any'
    AND dr.hepatic_function = 'any'
    AND dr.pregnancy_status = 'any'
    AND dr.dose_basis       IS NOT DISTINCT FROM bf.best_dose_basis
    AND dr.frequency        IS NOT NULL
    AND UPPER(COALESCE(dr.dose_amount, '')) != 'CONTRAINDICATED'
    AND (
      $2::text[] && ARRAY['pediatric','neonate','infant']
      OR dr.administration_notes NOT ILIKE '%pediatric%'
      OR dr.administration_notes IS NULL
    )
)
SELECT
  bf.formulation_id,
  ib.brand_name,
  ib.salt_composition,
  (
    SELECT STRING_AGG(i.name, ' / ' ORDER BY i.name)
    FROM drugdb.drug_ingredient_mapping dim
    JOIN drugdb.ingredients i ON i.id = dim.ingredient_id
    WHERE dim.formulation_id = bf.formulation_id
  ) AS generic_name,
  r.frequency,
  r.route,
  r.dose_amount,
  r.dose_unit,
  r.duration,
  LOWER(r.indication)      AS indication,
  r.administration_notes   AS instructions
FROM ranked r
CROSS JOIN best_formulation bf
JOIN drugdb.indian_brand ib
  ON ib.drug_id_1mg = $1
  AND ib.match_combination NOT IN ('drugbank', 'us_unapproved')
WHERE r.rn = 1
ORDER BY r.frequency, r.dose_value;
