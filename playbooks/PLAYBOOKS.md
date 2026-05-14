# TST2SK Playbooks

## General Recipes

### Element Visibility
- If element is present in DOM but not visible:
  - Check `opacity`, `visibility`, `display`.
  - Check `z-index` and `clip-path`.
  - Check for overlapping elements using `getBoundingClientRect`.

### Interaction Issues
- If button is disabled:
  - Check `disabled` attribute.
  - Check for pointer-events: none.
  - Check for transparent overlays.
