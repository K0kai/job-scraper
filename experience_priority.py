"""Regra compartilhada: como citar experiências em respostas de candidatura.

LLMs às vezes fixam num único papel (o com mais métricas ou o mais recente)
e ignoram o outro. A regra pede cobertura equilibrada, sem forçar prioridade
do emprego atual.
"""

EXPERIENCE_PRIORITY_RULE = """
Experience coverage (CRITICAL — follow every time):
- Do NOT systematically ignore any role on the resume. If the candidate has
  multiple jobs, answers and letters should be able to draw on more than one
  when relevant — never write as if only a single employer existed.
- Choose what to emphasize by FIT to the question/job (stack, domain, impact),
  not by which role has more numbers and not by “always current” or “always oldest”.
- A strong past role (e.g. heavy automation impact) is worth mentioning when it
  supports the answer; the current role is worth mentioning when it shows what
  they do now / stack / employer. Prefer weaving both briefly over picking one
  and erasing the other.
- Still: never invent employers, dates, metrics, or skills. If space is tight
  (short form field), pick the single best-fitting fact — but across typical
  open answers and cover letters, avoid always defaulting to the same employer.
""".strip()
