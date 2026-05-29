"""
Дефолтные схемы контрактов для типовых артефактов
"""
from typing import Dict, Any


def get_text_output_schema() -> Dict[str, Any]:
    """Схема для текстовых выходов"""
    return {
        "name": "text_output",
        "version": "1.0",
        "schema": {
            "type": "string",
            "minLength": 10
        },
        "business_rules": [
            "no_empty_output",
            "meaningful_content"
        ],
        "quality_thresholds": {
            "min_score": 0.7,
            "completeness": 0.8
        },
        "validators": ["structural", "completeness", "security"]
    }


def get_sql_query_schema() -> Dict[str, Any]:
    """Схема для SQL запросов"""
    return {
        "name": "sql_query",
        "version": "1.0", 
        "schema": {
            "type": "object",
            "required": ["query", "explanation"],
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 10,
                    "pattern": "^(SELECT|WITH).*"
                },
                "explanation": {
                    "type": "string",
                    "minLength": 50
                },
                "tables_used": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "estimated_rows": {
                    "type": "integer",
                    "minimum": 0
                }
            }
        },
        "business_rules": [
            "query_must_be_safe",
            "no_delete_or_drop_statements",
            "explanation_must_match_query",
            "performance_acceptable"
        ],
        "quality_thresholds": {
            "min_score": 0.9,
            "completeness": 0.95
        },
        "validators": ["structural", "completeness", "security"]
    }


def get_analysis_report_schema() -> Dict[str, Any]:
    """Схема для аналитических отчетов"""
    return {
        "name": "analysis_report",
        "version": "1.0",
        "schema": {
            "type": "object",
            "required": ["summary", "findings", "methodology"],
            "properties": {
                "summary": {
                    "type": "string",
                    "minLength": 100
                },
                "findings": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["finding", "confidence"],
                        "properties": {
                            "finding": {"type": "string"},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "supporting_data": {"type": "string"}
                        }
                    }
                },
                "methodology": {
                    "type": "string",
                    "minLength": 50
                },
                "recommendations": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "data_sources": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            }
        },
        "business_rules": [
            "findings_must_be_supported",
            "recommendations_actionable",
            "methodology_clear"
        ],
        "quality_thresholds": {
            "min_score": 0.8,
            "completeness": 0.9
        },
        "validators": ["structural", "completeness", "semantic"]
    }


def get_research_output_schema() -> Dict[str, Any]:
    """Схема для результатов исследований"""
    return {
        "name": "research_output",
        "version": "1.0",
        "schema": {
            "type": "object",
            "required": ["topic", "key_findings", "sources"],
            "properties": {
                "topic": {
                    "type": "string",
                    "minLength": 5
                },
                "key_findings": {
                    "type": "array",
                    "minItems": 3,
                    "items": {"type": "string", "minLength": 20}
                },
                "sources": {
                    "type": "array",
                    "minItems": 2,
                    "items": {
                        "type": "object",
                        "required": ["title", "url", "relevance"],
                        "properties": {
                            "title": {"type": "string"},
                            "url": {"type": "string", "format": "uri"},
                            "relevance": {"type": "number", "minimum": 0, "maximum": 1},
                            "date": {"type": "string"},
                            "summary": {"type": "string"}
                        }
                    }
                },
                "confidence_level": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1
                }
            }
        },
        "business_rules": [
            "sources_must_be_credible",
            "findings_must_be_relevant",
            "minimum_source_diversity"
        ],
        "quality_thresholds": {
            "min_score": 0.75,
            "completeness": 0.85
        },
        "validators": ["structural", "completeness", "security"]
    }


def get_default_schemas() -> Dict[str, Dict[str, Any]]:
    """Получить все дефолтные схемы"""
    return {
        "text_output": get_text_output_schema(),
        "sql_query": get_sql_query_schema(),
        "analysis_report": get_analysis_report_schema(),
        "research_output": get_research_output_schema()
    }
