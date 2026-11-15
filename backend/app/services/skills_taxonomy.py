"""Versioned skill taxonomy (`skills-v2`): canonical names and the surface forms that map to
them.

Used in two places:

- `canonical_skill` maps a candidate fact or a structured requirement to its canonical name,
  so "Postgres" and "PostgreSQL" compare equal.
- The job-description text extractor (`app.services.jd_text`) finds these skills in free
  text. Every pattern is matched case-insensitively at word boundaries, except where a
  pattern is marked case-sensitive because the word is common in English ("Go", "Swift",
  "Helm", "Jest" only match when capitalized).

Adding a skill or alias changes extraction results, so it needs a new taxonomy version.
Ambiguous one-letter languages (R, C) are deliberately absent: they cannot be found in prose
without guessing.
"""

import re
from dataclasses import dataclass

TAXONOMY_VERSION = "skills-v2"


@dataclass(frozen=True)
class SkillEntry:
    canonical: str
    aliases: tuple[str, ...]
    case_sensitive: tuple[str, ...] = ()


_ENTRIES: tuple[SkillEntry, ...] = (
    # Languages
    SkillEntry("Python", ("python", "py")),
    SkillEntry("Java", ("java",)),
    SkillEntry("JavaScript", ("javascript", "js", "ecmascript")),
    SkillEntry("TypeScript", ("typescript", "ts")),
    SkillEntry("Go", ("golang",), case_sensitive=("Go",)),
    SkillEntry("Rust", (), case_sensitive=("Rust",)),
    SkillEntry("C++", ("c++", "cpp")),
    SkillEntry("C#", ("c#", "csharp")),
    SkillEntry("Ruby", ("ruby",)),
    SkillEntry("PHP", ("php",)),
    SkillEntry("Kotlin", ("kotlin",)),
    SkillEntry("Swift", (), case_sensitive=("Swift",)),
    SkillEntry("Scala", ("scala",)),
    SkillEntry("Elixir", ("elixir",)),
    SkillEntry("SQL", ("sql",)),
    SkillEntry("Bash", ("bash", "shell scripting")),
    # Web and application frameworks
    SkillEntry("React", ("react", "react.js", "reactjs")),
    SkillEntry("Next.js", ("next.js", "nextjs")),
    SkillEntry("Vue.js", ("vue", "vue.js", "vuejs")),
    SkillEntry("Angular", ("angular",)),
    SkillEntry("Svelte", ("svelte",)),
    SkillEntry("Node.js", ("node.js", "nodejs")),
    SkillEntry("Express", ("express.js", "expressjs")),
    SkillEntry("Django", ("django",)),
    SkillEntry("Flask", ("flask",)),
    SkillEntry("FastAPI", ("fastapi",)),
    SkillEntry("Spring", ("spring boot", "spring framework")),
    SkillEntry("Ruby on Rails", ("ruby on rails",)),
    SkillEntry(".NET", (".net", "dotnet", "asp.net")),
    SkillEntry("Laravel", ("laravel",)),
    SkillEntry("GraphQL", ("graphql",)),
    SkillEntry("REST APIs", ("rest api", "rest apis", "restful", "restful apis")),
    SkillEntry("gRPC", ("grpc",)),
    SkillEntry("HTML", ("html", "html5")),
    SkillEntry("CSS", ("css", "css3")),
    SkillEntry("Tailwind CSS", ("tailwind", "tailwind css")),
    # Mobile
    SkillEntry("React Native", ("react native",)),
    SkillEntry("Flutter", ("flutter",)),
    SkillEntry("Android", ("android",)),
    SkillEntry("iOS", ("ios",)),
    # Data and machine learning
    SkillEntry("pandas", ("pandas",)),
    SkillEntry("NumPy", ("numpy",)),
    SkillEntry("scikit-learn", ("scikit-learn", "sklearn")),
    SkillEntry("TensorFlow", ("tensorflow",)),
    SkillEntry("PyTorch", ("pytorch",)),
    SkillEntry("Machine Learning", ("machine learning",), case_sensitive=("ML",)),
    SkillEntry("Deep Learning", ("deep learning",)),
    SkillEntry("NLP", ("nlp", "natural language processing")),
    SkillEntry("Computer Vision", ("computer vision",)),
    SkillEntry("LLMs", ("llm", "llms", "large language models")),
    SkillEntry("Apache Spark", ("apache spark", "spark", "pyspark")),
    SkillEntry("Apache Kafka", ("apache kafka", "kafka")),
    SkillEntry("Apache Airflow", ("apache airflow", "airflow")),
    SkillEntry("dbt", ("dbt",)),
    SkillEntry("Hadoop", ("hadoop",)),
    SkillEntry("Snowflake", ("snowflake",)),
    SkillEntry("BigQuery", ("bigquery",)),
    SkillEntry("Redshift", ("redshift",)),
    SkillEntry("Databricks", ("databricks",)),
    SkillEntry("ETL", ("etl", "elt")),
    SkillEntry("Data Modeling", ("data modeling", "data modelling")),
    SkillEntry("Statistics", ("statistics", "statistical analysis")),
    SkillEntry("Excel", ("excel", "microsoft excel", "ms excel")),
    SkillEntry("Tableau", ("tableau",)),
    SkillEntry("Power BI", ("power bi", "powerbi")),
    SkillEntry("Looker", ("looker",)),
    # Databases and storage
    SkillEntry("PostgreSQL", ("postgresql", "postgres")),
    SkillEntry("MySQL", ("mysql",)),
    SkillEntry("SQLite", ("sqlite",)),
    SkillEntry("Microsoft SQL Server", ("sql server", "mssql")),
    SkillEntry("Oracle Database", ("oracle database", "oracle db")),
    SkillEntry("MongoDB", ("mongodb", "mongo")),
    SkillEntry("Redis", ("redis",)),
    SkillEntry("Elasticsearch", ("elasticsearch", "opensearch")),
    SkillEntry("Cassandra", ("cassandra",)),
    SkillEntry("DynamoDB", ("dynamodb",)),
    # Cloud, infrastructure, and operations
    SkillEntry("AWS", ("aws", "amazon web services")),
    SkillEntry("Google Cloud", ("gcp", "google cloud", "google cloud platform")),
    SkillEntry("Azure", ("azure", "microsoft azure")),
    SkillEntry("Docker", ("docker",)),
    SkillEntry("Kubernetes", ("kubernetes", "k8s")),
    SkillEntry("Terraform", ("terraform",)),
    SkillEntry("Ansible", ("ansible",)),
    SkillEntry("Helm", (), case_sensitive=("Helm",)),
    SkillEntry("Linux", ("linux",)),
    SkillEntry("CI/CD", ("ci/cd", "continuous integration", "continuous delivery")),
    SkillEntry("GitHub Actions", ("github actions",)),
    SkillEntry("Jenkins", ("jenkins",)),
    SkillEntry("Git", ("git",)),
    SkillEntry("Prometheus", ("prometheus",)),
    SkillEntry("Grafana", ("grafana",)),
    SkillEntry("Observability", ("observability",)),
    SkillEntry("Microservices", ("microservices", "micro-services")),
    SkillEntry("Distributed Systems", ("distributed systems",)),
    SkillEntry("System Design", ("system design",)),
    SkillEntry("Security", ("application security", "appsec", "cybersecurity")),
    # Testing and practices
    SkillEntry("Automated Testing", ("automated testing", "test automation", "unit testing")),
    SkillEntry("Playwright", ("playwright",)),
    SkillEntry("Selenium", ("selenium",)),
    SkillEntry("Cypress", ("cypress",)),
    SkillEntry("Jest", (), case_sensitive=("Jest",)),
    SkillEntry("pytest", ("pytest",)),
    SkillEntry("Agile", ("agile", "scrum", "kanban")),
    # Product, design, and business tools
    SkillEntry("Figma", ("figma",)),
    SkillEntry("Jira", ("jira",)),
    SkillEntry("Salesforce", ("salesforce",)),
    SkillEntry("SAP", ("sap",)),
    SkillEntry("Product Management", ("product management",)),
    SkillEntry("Project Management", ("project management",)),
    SkillEntry("Stakeholder Management", ("stakeholder management",)),
    SkillEntry("Technical Writing", ("technical writing",)),
    SkillEntry("Communication", ("communication skills", "written communication")),
)

_BY_ALIAS: dict[str, str] = {}
for _entry in _ENTRIES:
    _BY_ALIAS[_entry.canonical.lower()] = _entry.canonical
    for _alias in _entry.aliases:
        _BY_ALIAS[_alias.lower()] = _entry.canonical


def canonical_name(value: str) -> str | None:
    """The canonical name for a known skill or alias, else None."""
    return _BY_ALIAS.get(re.sub(r"\s+", " ", value.strip().lower()))


def _pattern(alias: str) -> str:
    # Word boundaries that also work for aliases ending in symbols ("C++", "C#", ".NET").
    return rf"(?<![\w.+#/-]){re.escape(alias)}(?![\w+#]|\.\w)"


def _compile() -> list[tuple[str, re.Pattern[str]]]:
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for entry in _ENTRIES:
        exact_only = {word.lower() for word in entry.case_sensitive}
        aliases = sorted(
            {entry.canonical.lower(), *entry.aliases} - exact_only, key=len, reverse=True
        )
        parts = [_pattern(alias) for alias in aliases]
        if parts:
            compiled.append((entry.canonical, re.compile("|".join(parts), re.IGNORECASE)))
        for word in entry.case_sensitive:
            compiled.append((entry.canonical, re.compile(_pattern(word))))
    return compiled


_PATTERNS = _compile()


def find_skills(text: str) -> list[str]:
    """Canonical skills mentioned in `text`, in order of first appearance, without repeats.

    A longer skill that contains a shorter one ("React Native" and "React") reports both
    only when the shorter one also appears on its own.
    """
    spans: list[tuple[int, int, str]] = []
    for canonical, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end(), canonical))
    # Drop a match fully covered by a longer match of a different skill.
    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    kept: list[tuple[int, int, str]] = []
    for start, end, canonical in spans:
        if any(
            other_start <= start and end <= other_end and other != canonical
            for other_start, other_end, other in kept
        ):
            continue
        kept.append((start, end, canonical))
    seen: dict[str, int] = {}
    for start, _end, canonical in kept:
        seen.setdefault(canonical, start)
    return sorted(seen, key=lambda name: seen[name])
