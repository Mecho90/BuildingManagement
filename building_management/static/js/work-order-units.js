(function () {
  const initUnitsWidget = function () {
    const unitField = document.querySelector("select[data-units-api-template]");
    if (!unitField) {
      return;
    }

    const unitsApiTemplate = unitField.dataset.unitsApiTemplate;
    if (!unitsApiTemplate) {
      return;
    }

    const buildingField = document.getElementById("id_building");
    const loadingText = unitField.dataset.loadingText || "Loading…";
    const unitWrapper = document.querySelector(".js-work-order-unit-field");
    const employeeWrapper = document.querySelector(".js-work-order-employee-field");
    const employeeField = document.getElementById("id_office_employee");
    const officeBuildingId = buildingField ? buildingField.dataset.officeBuildingId || "" : "";

    const isOfficeBuilding = function (buildingId) {
      if (!officeBuildingId || !buildingId) {
        return false;
      }
      return String(officeBuildingId) === String(buildingId);
    };

    const toggleOfficeFields = function (buildingId) {
      const officeSelected = isOfficeBuilding(buildingId);
      if (unitWrapper) {
        unitWrapper.classList.toggle("hidden", officeSelected);
      }
      if (employeeWrapper) {
        employeeWrapper.classList.toggle("hidden", !officeSelected);
      }
      if (employeeField) {
        employeeField.disabled = !officeSelected;
        employeeField.setAttribute("aria-disabled", String(!officeSelected));
        if (!officeSelected) {
          employeeField.value = "";
        }
      }
      if (officeSelected) {
        setDisabledState(true, unitField.getAttribute("data-empty-label") || "---------");
      }
      return officeSelected;
    };

    const setDisabledState = function (disabled, message) {
      unitField.innerHTML = "";
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = message || "---------";
      unitField.appendChild(placeholder);
      unitField.value = "";
      unitField.disabled = disabled;
      unitField.setAttribute("aria-disabled", String(disabled));
      unitField.setAttribute("aria-busy", String(disabled));
    };

    const populateUnits = async function (buildingId, selectedId) {
      if (!buildingId) {
        setDisabledState(true, unitField.getAttribute("data-empty-label") || "---------");
        return;
      }

      setDisabledState(true, loadingText);

      const endpoint = unitsApiTemplate.replace("{id}", encodeURIComponent(buildingId));
      try {
        const response = await fetch(endpoint, { headers: { Accept: "application/json" } });
        if (!response.ok) {
          throw new Error(`Unable to fetch units for building ${buildingId}`);
        }
        const data = await response.json();
        unitField.innerHTML = "";

        const blankOption = document.createElement("option");
        blankOption.value = "";
        blankOption.textContent = "---------";
        unitField.appendChild(blankOption);

        data.forEach(function (unit) {
          const option = document.createElement("option");
          option.value = unit.id;
          option.textContent = unit.label || unit.number || `#${unit.id}`;
          if (String(unit.id) === String(selectedId)) {
            option.selected = true;
          }
          unitField.appendChild(option);
        });

        unitField.disabled = data.length === 0;
        unitField.setAttribute("aria-disabled", String(unitField.disabled));
      } catch (error) {
        console.warn(error);
        setDisabledState(true, "—");
      } finally {
        unitField.removeAttribute("aria-busy");
      }
    };

    const initialBuilding =
      unitField.dataset.initialBuilding || (buildingField ? buildingField.value : "");
    const selectedUnit = unitField.dataset.selectedUnit || unitField.value;

    const initialIsOffice = toggleOfficeFields(initialBuilding);
    if (initialBuilding && !initialIsOffice) {
      populateUnits(initialBuilding, selectedUnit);
    } else {
      setDisabledState(true, unitField.getAttribute("data-empty-label") || "---------");
    }

    if (buildingField && buildingField.tagName !== "INPUT") {
      buildingField.addEventListener("change", function (event) {
        const selectedBuilding = event.target.value;
        const officeSelected = toggleOfficeFields(selectedBuilding);
        if (officeSelected) {
          return;
        }
        populateUnits(selectedBuilding, "");
      });
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initUnitsWidget);
  } else {
    initUnitsWidget();
  }
})();
