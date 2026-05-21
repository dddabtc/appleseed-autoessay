import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  // PR-209 follow-up: `_login_design_ref/` is the user-supplied
  // static design pack kept in-repo as visual reference (not shipped
  // to the bundle). Skip lint — its JS uses raw browser globals
  // (document / FormData / console) which the project's TS-only
  // browser-globals block doesn't cover.
  { ignores: ['dist', '_login_design_ref/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }]
    }
  }
);
