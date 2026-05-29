#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Система оптимизации промптов агентов MultiAgent

Модуль предоставляет инструменты для автоматической оптимизации промптов
агентов на основе типа используемой модели и лучших практик из OpenAI Cookbook.

Основные компоненты:
- PromptOptimizer: Главный класс для оптимизации промптов
- optimize_agents.py: CLI утилита для запуска оптимизации
- restore_agents.py: Утилита для восстановления из резервных копий

Пример использования:
    from prompt_optimizer import PromptOptimizer
    
    optimizer = PromptOptimizer()
    results = optimizer.optimize_all_agents()
"""

from .prompt_optimizer import PromptOptimizer

__version__ = "1.0.0"
__author__ = "MultiAgent System"
__all__ = ["PromptOptimizer"]
