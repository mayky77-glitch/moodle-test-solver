from __future__ import annotations

from .models import PageQuestion


READ_QUESTIONS_SCRIPT = """
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
  const cleanQuestionText = (text) => {
    const junkPatterns = [
      /^текст вопроса$/i,
      /^вопрос\\s*\\d+$/i,
      /^пока нет ответа$/i,
      /^ответ сохранен$/i,
      /^балл\\s*:/i,
      /^отметить вопрос$/i,
      /^вопрос\\s*\\d+\\s+ответ$/i,
    ];
    return String(text || '')
      .split(/\\n| {2,}/)
      .map(line => line.replace(/\\s+/g, ' ').trim())
      .filter(line => line && !junkPatterns.some(pattern => pattern.test(line)))
      .join(' ')
      .trim();
  };
  const questionTextFrom = (root) => {
    const qtext = root.querySelector('.qtext');
    if (qtext) {
      const direct = cleanQuestionText(textOf(qtext));
      if (direct.length > 8) return direct;
    }
    const preferredNodes = Array.from(root.querySelectorAll('.qtext p, .qtext div, .formulation p, .formulation div')).filter(visible);
    const preferred = preferredNodes.map(node => cleanQuestionText(textOf(node))).find(text => text.length > 8);
    if (preferred) return preferred;
    const fallbackNodes = Array.from(root.querySelectorAll('legend,.prompt,[data-question],h2,h3,h4')).filter(visible);
    const fallback = fallbackNodes.map(node => cleanQuestionText(textOf(node))).find(text => text.length > 8);
    if (fallback) return fallback;
    return cleanQuestionText(textOf(root)).slice(0, 1200);
  };
  const selectorFor = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const name = el.getAttribute('name');
    if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
    const dataId = el.getAttribute('data-testid') || el.getAttribute('data-test');
    if (dataId) return `[data-testid="${CSS.escape(dataId)}"],[data-test="${CSS.escape(dataId)}"]`;
    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 5) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(child => child.tagName === node.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  };

  const readFrom = (root) => {
    const inputs = Array.from(root.querySelectorAll('input, textarea, select')).filter(visible);
    const choiceInputs = inputs.filter(input => ['radio', 'checkbox'].includes((input.type || '').toLowerCase()));
    const textInputs = inputs.filter(input => ['text', 'search', ''].includes((input.type || '').toLowerCase()) || input.tagName.toLowerCase() === 'textarea');

    const optionSelectors = {};
    const options = choiceInputs.map((input) => {
      const label = input.closest('label') || document.querySelector(`label[for="${CSS.escape(input.id || '')}"]`);
      const container = input.closest('li, .answer, .option, .form-check, .r0, .r1, label, div') || input.parentElement;
      let labelText = textOf(label || container || input);
      labelText = labelText.replace(/^(a|b|c|d|e|f|g|h|i|j|[а-яё])\\.|^\\d+[.)]/i, '').trim();
      optionSelectors[labelText] = selectorFor(input);
      return labelText;
    }).filter(Boolean);

    const questionText = questionTextFrom(root);

    let kind = 'unknown';
    if (choiceInputs.some(input => input.type === 'checkbox')) kind = 'multiple_choice';
    else if (choiceInputs.some(input => input.type === 'radio')) kind = 'single_choice';
    else if (textInputs.length) kind = 'text';

    return {
      text: questionText,
      kind,
      options,
      option_selectors: optionSelectors,
      input_selector: textInputs[0] ? selectorFor(textInputs[0]) : null
    };
  };

  const buttons = Array.from(document.querySelectorAll('button,input[type="submit"],input[type="button"],a')).filter(visible);
  const buttonSelector = (patterns) => {
    const found = buttons.find(button => patterns.some(pattern => pattern.test(textOf(button) || button.value || button.name || '')));
    return found ? selectorFor(found) : null;
  };

  const moodleQuestions = Array.from(document.querySelectorAll('.que')).filter(visible);
  const roots = moodleQuestions.length
    ? moodleQuestions
    : [document.querySelector('form') || document.body];

  return roots.map(readFrom).filter(item => item.text || item.options.length).map(item => ({
    ...item,
    submit_selector: buttonSelector([/ответ/i, /submit/i, /провер/i, /сохран/i, /finish attempt/i, /закончить/i]),
    next_selector: buttonSelector([/далее/i, /next/i, /след/i, /continue/i, /^next$/i])
  }));
}
"""


async def read_questions(page) -> list[PageQuestion]:
    items = await page.evaluate(READ_QUESTIONS_SCRIPT)
    return [
        PageQuestion(
            text=item.get("text") or "",
            kind=item.get("kind") or "unknown",
            options=list(dict.fromkeys(item.get("options") or [])),
            input_selector=item.get("input_selector"),
            option_selectors=item.get("option_selectors") or {},
            submit_selector=item.get("submit_selector"),
            next_selector=item.get("next_selector"),
        )
        for item in items
    ]


async def read_question(page) -> PageQuestion:
    questions = await read_questions(page)
    return questions[0] if questions else PageQuestion(text="")
