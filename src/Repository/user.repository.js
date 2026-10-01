const prisma = require("../config/prismaClient.js");


const findAll = () => {
    return prisma.user.findMany({
        orderBy: {
            createdAt: "desc"
        }
    })
}


const findById = (id) => {
    console.log(id)
    return prisma.user.findUnique({
        where: {
            id
        }
    });
};


const findEmail = (email) => {
    return prisma.user.findUnique({
        where: {
            email
        }
    });
}


const createUser = (data) => {

    return prisma.user.create({
        data
    });
};


const update = (id, data) => {

    return prisma.user.update({
        where: { id },
        data
    })
}


const deleteUser = (id) => {
    return prisma.user.delete({
        where: { id }
    })
}


module.exports = {
    findAll,
    findById,
    findEmail,
    createUser,
    update,
    deleteUser
}