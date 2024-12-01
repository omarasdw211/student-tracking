// البرامج المتاحة للمتابعة
const programs = [
    'البرنامج الأول',
    'البرنامج الثاني',
    'البرنامج الثالث',
    'البرنامج الرابع',
    'البرنامج الخامس'
];

// تخزين بيانات الطلاب
let students = {
    group1: [],
    group2: [],
    group3: []
};

// تحميل البيانات المحفوظة مسبقاً
function loadData() {
    const savedData = localStorage.getItem('studentsData');
    if (savedData) {
        students = JSON.parse(savedData);
        renderAllGroups();
    }
}

// حفظ البيانات
function saveData() {
    localStorage.setItem('studentsData', JSON.stringify(students));
    alert('تم حفظ البيانات بنجاح');
}

// إضافة طالب جديد
function addStudent() {
    const studentName = prompt('أدخل اسم الطالب:');
    if (!studentName) return;

    const groupNumber = prompt('أدخل رقم المجموعة (1-3):');
    if (![1,2,3].includes(Number(groupNumber))) {
        alert('رقم المجموعة غير صحيح');
        return;
    }

    const newStudent = {
        id: Date.now(),
        name: studentName,
        programs: programs.reduce((acc, prog) => {
            acc[prog] = false;
            return acc;
        }, {})
    };

    students[`group${groupNumber}`].push(newStudent);
    renderGroup(groupNumber);
    saveData();
}

// حذف طالب
function deleteStudent(groupNumber, studentId) {
    const groupKey = `group${groupNumber}`;
    students[groupKey] = students[groupKey].filter(student => student.id !== studentId);
    renderGroup(groupNumber);
    saveData();
}

// تحديث حالة البرنامج
function toggleProgram(groupNumber, studentId, programName) {
    const student = students[`group${groupNumber}`].find(s => s.id === studentId);
    if (student) {
        student.programs[programName] = !student.programs[programName];
        saveData();
    }
}

// عرض مجموعة معينة
function renderGroup(groupNumber) {
    const groupElement = document.getElementById(`group${groupNumber}`);
    const groupStudents = students[`group${groupNumber}`];

    groupElement.innerHTML = groupStudents.map(student => `
        <div class="student-card">
            <button class="delete-btn" onclick="deleteStudent(${groupNumber}, ${student.id})">حذف</button>
            <div class="student-name">${student.name}</div>
            <div class="programs">
                ${programs.map(program => `
                    <label class="program-check">
                        <input type="checkbox" 
                            ${student.programs[program] ? 'checked' : ''}
                            onchange="toggleProgram(${groupNumber}, ${student.id}, '${program}')"
                        >
                        ${program}
                    </label>
                `).join('')}
            </div>
        </div>
    `).join('');
}

// عرض جميع المجموعات
function renderAllGroups() {
    for (let i = 1; i <= 3; i++) {
        renderGroup(i);
    }
}

// تحميل البيانات عند بدء التطبيق
loadData();
